import getpass
import os
import shutil
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.audit.service import audit
from apps.transfer import archive, state

PASSWORD_ENV = "DOCNEST_TRANSFER_PASSWORD"  # noqa: S105 - the variable name, not a password


class Command(BaseCommand):
    help = (
        "Replace this instance with an export: database, master key, Proton Drive sign-in and files. "
        "Everything currently on this instance is lost."
    )

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("archive", help="Path of the export ZIP, or - to read it from stdin")
        parser.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
        parser.add_argument("--keep", action="store_true", help="Keep the ZIP after a successful import")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        path, received = self._archive(options["archive"])
        if options["password_stdin"] and options["archive"] != "-":
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = os.environ.get(PASSWORD_ENV) or getpass.getpass("Password of the export: ")
        try:
            manifest = archive.inspect(path, password)
        except state.TransferError as exc:
            raise CommandError(str(exc)) from exc

        counts = manifest.get("counts", {})
        self.stdout.write(
            f"Export of {manifest.get('base_url')} from {manifest.get('created_at')} "
            f"(DocNest {manifest.get('docnest_version')}): {counts.get('documents', '?')} documents, "
            f"{counts.get('users', '?')} users."
        )
        self.stdout.write(
            "The accounts of this server are kept; the export's accounts are not taken over."
            if manifest["keeps_accounts"]
            else "This server has no users yet: the accounts of the export are taken over."
        )
        for note in manifest["warnings"]:
            self.stdout.write(self.style.WARNING(f"! {note}"))
        if not options["yes"]:
            answer = input("Everything on this instance will be replaced. Type IMPORT to continue: ")
            if answer.strip() != "IMPORT":
                raise CommandError("Cancelled")

        try:
            with state.exclusive("import"):
                notes = archive.restore(path, password)
                restarted = archive.restart_services()
                if not restarted:
                    notes.append(
                        "Restart DocNest now (docker compose restart app) so it uses the imported data."
                    )
                state.write_status(warnings=notes, restarting=restarted)
        except state.TransferError as exc:
            raise CommandError(str(exc)) from exc
        audit("system.imported", exported_at=manifest.get("created_at"), source=manifest.get("base_url"))
        if received or not options["keep"]:
            path.unlink(missing_ok=True)
        for note in notes[len(manifest["warnings"]) :]:  # the warnings were shown before
            self.stdout.write(self.style.WARNING(f"! {note}"))
        self.stdout.write(
            self.style.SUCCESS("Import complete." + (" DocNest restarts now." if restarted else ""))
        )

    def _archive(self, arg: str) -> tuple[Path, bool]:
        if arg != "-":
            path = Path(arg)
            if not path.is_file():
                raise CommandError(f"{arg} does not exist")
            return path, False
        if sys.stdin.isatty():
            raise CommandError("Pipe the export in: docker compose exec -T app docnest import - < export.zip")
        path = state.directory() / "import-stdin.zip"
        with path.open("wb") as out:
            shutil.copyfileobj(sys.stdin.buffer, out, 1024 * 1024)
        return path, True
