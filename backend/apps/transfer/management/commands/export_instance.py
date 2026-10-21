import getpass
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.audit.service import audit
from apps.transfer import access, archive, state


class Command(BaseCommand):
    help = (
        "Export the whole instance (database, master key, Proton Drive sign-in, files not yet in "
        "Proton Drive) into one password-protected ZIP."
    )

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--output", help="Where to write the ZIP (default: the transfer folder)")
        parser.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        if options["password_stdin"]:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password for the export: ")
            if getpass.getpass("Repeat the password: ") != password:
                raise CommandError("The passwords do not match")
        if problems := access.password_problems(password):
            raise CommandError(" ".join(problems))
        dest = Path(options["output"]) if options["output"] else state.directory() / archive.export_name()
        state.cleanup_exports()
        try:
            with state.exclusive("export"):
                manifest = archive.export(dest, password)
                state.write_status(export={"name": dest.name, "size": dest.stat().st_size, "path": str(dest)})
        except state.TransferError as exc:
            raise CommandError(str(exc)) from exc
        audit("system.exported", documents=manifest["counts"]["documents"], size=dest.stat().st_size)
        self.stdout.write(f"Export written: {dest} ({dest.stat().st_size:,} bytes)")
        if not options["output"]:
            self.stdout.write(f"Copy it off the server with:  docker compose cp app:{dest} .")
