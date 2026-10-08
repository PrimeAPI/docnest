from __future__ import annotations

import threading
from collections.abc import Sequence
from types import SimpleNamespace

from apps.processing.models import Job
from apps.processing.worker import Worker


def test_worker_respects_live_processing_concurrency(monkeypatch):
    worker = Worker()
    jobs = [
        SimpleNamespace(pk=1, kind=Job.Kind.PROCESS_DOCUMENT),
        SimpleNamespace(pk=2, kind=Job.Kind.PROCESS_DOCUMENT),
    ]
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    peak = 0
    completed = 0

    def claim(_worker_id: str, *, kinds: Sequence[str] | None = None):
        return jobs.pop(0) if jobs else None

    def execute(_job: object) -> None:
        nonlocal active, peak, completed
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
            completed += 1
            if completed == 2:
                worker.stopping = True

    monkeypatch.setattr("apps.processing.worker.signal.signal", lambda *_args: None)
    monkeypatch.setattr("apps.processing.worker.ensure_dirs", lambda: None)
    monkeypatch.setattr("apps.processing.worker.get_processing_concurrency", lambda: 2)
    monkeypatch.setattr("apps.processing.worker.queue.claim", claim)
    monkeypatch.setattr(worker, "sweep_workspace", lambda: None)
    monkeypatch.setattr(worker, "periodic", lambda: None)
    monkeypatch.setattr(worker, "execute", execute)

    worker.run_forever()

    assert completed == 2
    assert peak == 2


def test_worker_finishes_maintenance_before_parallel_document_jobs(monkeypatch):
    worker = Worker()
    jobs = [
        SimpleNamespace(pk=1, kind=Job.Kind.TRAIN_CLASSIFIER),
        SimpleNamespace(pk=2, kind=Job.Kind.PROCESS_DOCUMENT),
        SimpleNamespace(pk=3, kind=Job.Kind.PROCESS_DOCUMENT),
    ]
    barrier = threading.Barrier(2)
    maintenance_done = False
    completed_documents = 0
    lock = threading.Lock()

    def claim(_worker_id: str, *, kinds: Sequence[str] | None = None):
        for index, job in enumerate(jobs):
            if kinds is None or job.kind in kinds:
                return jobs.pop(index)
        return None

    def execute(job: SimpleNamespace) -> None:
        nonlocal maintenance_done, completed_documents
        if job.kind == Job.Kind.TRAIN_CLASSIFIER:
            maintenance_done = True
            return
        assert maintenance_done
        barrier.wait(timeout=5)
        with lock:
            completed_documents += 1
            if completed_documents == 2:
                worker.stopping = True

    monkeypatch.setattr("apps.processing.worker.signal.signal", lambda *_args: None)
    monkeypatch.setattr("apps.processing.worker.ensure_dirs", lambda: None)
    monkeypatch.setattr("apps.processing.worker.get_processing_concurrency", lambda: 2)
    monkeypatch.setattr("apps.processing.worker.queue.claim", claim)
    monkeypatch.setattr(worker, "sweep_workspace", lambda: None)
    monkeypatch.setattr(worker, "periodic", lambda: None)
    monkeypatch.setattr(worker, "execute", execute)

    worker.run_forever()

    assert completed_documents == 2


def test_new_scans_are_taken_in_while_slow_processing_runs(monkeypatch):
    worker = Worker()
    jobs = [
        SimpleNamespace(pk=1, kind=Job.Kind.PROCESS_DOCUMENT),
        SimpleNamespace(pk=2, kind=Job.Kind.INTAKE_DOCUMENT),
    ]
    prepared = threading.Event()
    order: list[int] = []

    def claim(_worker_id: str, *, kinds: Sequence[str] | None = None):
        for index, job in enumerate(jobs):
            if kinds is None or job.kind in kinds:
                return jobs.pop(index)
        return None

    def execute(job: SimpleNamespace) -> None:
        if job.kind == Job.Kind.PROCESS_DOCUMENT:
            # A minutes-long AI analysis: it only finishes once the new scan was prepared.
            assert prepared.wait(timeout=5)
            order.append(job.pk)
            worker.stopping = True
        else:
            order.append(job.pk)
            prepared.set()

    monkeypatch.setattr("apps.processing.worker.signal.signal", lambda *_args: None)
    monkeypatch.setattr("apps.processing.worker.ensure_dirs", lambda: None)
    monkeypatch.setattr("apps.processing.worker.get_processing_concurrency", lambda: 1)
    monkeypatch.setattr("apps.processing.worker.queue.claim", claim)
    monkeypatch.setattr(worker, "sweep_workspace", lambda: None)
    monkeypatch.setattr(worker, "periodic", lambda: None)
    monkeypatch.setattr(worker, "execute", execute)

    worker.run_forever()

    assert order == [2, 1]


def test_processing_never_exceeds_the_limit_however_many_wait(monkeypatch):
    worker = Worker()
    jobs = [SimpleNamespace(pk=i, kind=Job.Kind.PROCESS_DOCUMENT) for i in range(1, 9)]
    lock = threading.Lock()
    active = peak = done = 0

    def claim(_worker_id: str, *, kinds: Sequence[str] | None = None):
        for index, job in enumerate(jobs):
            if kinds is None or job.kind in kinds:
                return jobs.pop(index)
        return None

    def execute(_job: object) -> None:
        nonlocal active, peak, done
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.05)
        with lock:
            active -= 1
            done += 1
            if done == 8:
                worker.stopping = True

    monkeypatch.setattr("apps.processing.worker.signal.signal", lambda *_args: None)
    monkeypatch.setattr("apps.processing.worker.ensure_dirs", lambda: None)
    monkeypatch.setattr("apps.processing.worker.get_processing_concurrency", lambda: 2)
    monkeypatch.setattr("apps.processing.worker.queue.claim", claim)
    monkeypatch.setattr(worker, "sweep_workspace", lambda: None)
    monkeypatch.setattr(worker, "periodic", lambda: None)
    monkeypatch.setattr(worker, "execute", execute)

    worker.run_forever()

    assert done == 8 and peak == 2
