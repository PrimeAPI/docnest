from django.core.management.base import BaseCommand

from apps.processing.worker import Worker


class Command(BaseCommand):
    help = "Process all queued jobs once and exit."

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        n = Worker().run_until_empty()
        self.stdout.write(f"Processed {n} job(s).")
