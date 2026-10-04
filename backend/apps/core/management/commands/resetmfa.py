from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import RecoveryCode, TotpDevice, User, UserSession, WebAuthnCredential
from apps.audit.service import audit


class Command(BaseCommand):
    help = "Remove all second factors of a user (account recovery). The user must enroll again."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("username")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        user = User.objects.filter(username__iexact=options["username"]).first()
        if user is None:
            raise CommandError("no such user")
        WebAuthnCredential.objects.filter(user=user).delete()
        TotpDevice.objects.filter(user=user).delete()
        RecoveryCode.objects.filter(user=user).delete()
        from django.contrib.sessions.models import Session

        keys = list(UserSession.objects.filter(user=user).values_list("session_key", flat=True))
        Session.objects.filter(session_key__in=keys).delete()
        UserSession.objects.filter(user=user).delete()
        audit("mfa.reset_by_admin", target=user.get_username())
        self.stdout.write(self.style.SUCCESS("Second factors removed; all sessions ended."))
