from django.core.management.base import BaseCommand, CommandError

from apps.storage import backup
from apps.storage.backends import StorageError


class Command(BaseCommand):
    help = "Back up the database to the storage backend now (Proton Drive: <root>/backups/)."

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        try:
            stored = backup.run()
        except (backup.BackupError, StorageError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Backup stored: {stored.path} ({stored.size:,} bytes)")
