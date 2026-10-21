from django.core.management.base import BaseCommand

from apps.transfer import access


class Command(BaseCommand):
    help = (
        "Print the setup code that a new instance (no users yet) asks for to import an export in the browser."
    )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        if not access.instance_empty():
            self.stdout.write(
                "This instance has users: sign in and import under Settings → System → Export & import."
            )
            return
        self.stdout.write(f"Setup code: {access.setup_code()}")
