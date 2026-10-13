"""Spawn-safe synthetic processing failures for supervisor integration tests."""


def _blocking_stage(document, work):
    """Simulate a native hang with an OCR grandchild and private plaintext temp files."""
    import os
    import subprocess
    import sys
    import time

    from django.conf import settings

    descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (work / "plaintext.pdf").write_bytes(b"private document")
    (settings.DATA_DIR / "runner-pid").write_text(str(os.getpid()))
    (settings.DATA_DIR / "started").write_text(str(descendant.pid))
    time.sleep(120)


def _blocking_child(job_id, lease_token, config, root, sender):
    import django
    from django.conf import settings

    for name, value in config.items():
        setattr(settings, name, value)
    django.setup()
    from apps.processing import execution, pipeline
    from apps.processing.models import Job

    job = Job.objects.get(pk=job_id)
    for stage in pipeline.PROCESSING_STAGES:
        pipeline.STAGES[stage] = _blocking_stage if stage == job.payload["block_stage"] else lambda *_: None
    execution._child(job_id, lease_token, config, root, sender)


def _crashing_child(*_args):
    import os
    import signal

    os.setsid()
    os.kill(os.getpid(), signal.SIGKILL)


def _supervisor_child(job_id, config):
    import django
    from django.conf import settings

    for name, value in config.items():
        setattr(settings, name, value)
    django.setup()
    from apps.processing import execution
    from apps.processing.models import Job

    execution._child = _blocking_child
    execution.run(Job.objects.get(pk=job_id))
