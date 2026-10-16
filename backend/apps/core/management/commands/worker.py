from django.core.management.base import BaseCommand

from apps.core.debuglog import log_startup
from apps.processing.worker import Worker


class Command(BaseCommand):
    help = "Run the background processing worker."

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        log_startup("worker")
        Worker().run_forever()
