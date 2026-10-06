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
