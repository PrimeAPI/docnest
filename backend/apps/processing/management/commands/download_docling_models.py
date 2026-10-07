from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.processing.docling_models import ModelDownloadFailed, ensure_models


class Command(BaseCommand):
    help = "Download Docling's standard and VLM models into the persistent cache."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--layout-only", action="store_true")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options) -> None:
        try:
            path = ensure_models(
                include_vlm=not options["layout_only"],
                force=options["force"],
            )
        except ModelDownloadFailed as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Docling models are ready in {path}"))
