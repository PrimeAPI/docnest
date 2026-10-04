from django.core.management.base import BaseCommand

from apps.processing.worker import Worker


class Command(BaseCommand):
    help = "Run the background processing worker."

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        Worker().run_forever()
