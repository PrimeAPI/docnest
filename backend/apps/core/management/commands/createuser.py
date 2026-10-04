import logging
import secrets
import string
import sys

from django.contrib.auth import password_validation
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User, UserSession
from apps.audit.service import audit

ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0OolI1") + "-_!?%+="


def generate_password(length: int = 24) -> str:
    """A random password that satisfies the password policy (≥ 20 chars, mixed classes)."""
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


class Command(BaseCommand):
    help = (
        "Create a user with a generated password, or — if the user exists — reset the password. "
        "The second factor is enrolled at first login; existing second factors are kept."
    )

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("username")
        parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="Read the password from stdin instead of generating one",
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        logging.getLogger(
            "docnest.audit"
        ).disabled = True  # keep the terminal output clean (still in the audit log)
        username = options["username"].strip()
        if not username or len(username) > 150:
            raise CommandError("invalid username")

        user = User.objects.filter(username__iexact=username).first()
        created = user is None
        if user is None:
            user = User(username=username)

        if options["password_stdin"]:
            password = sys.stdin.readline().rstrip("\n")
            try:
                password_validation.validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
        else:
            password = generate_password()

        user.set_password(password)
        user.is_active = True
        user.save()

        if created:
            audit("account.created", target=user.get_username())
            title = f"User '{user.get_username()}' created."
            hint = "Sign in and set up your second factor."
        else:
            # A password reset ends all existing sessions of that user.
            keys = list(UserSession.objects.filter(user=user).values_list("session_key", flat=True))
            Session.objects.filter(session_key__in=keys).delete()
            UserSession.objects.filter(user=user).delete()
            audit("account.password_reset_by_admin", target=user.get_username())
            title = f"Password of '{user.get_username()}' was reset. All sessions were signed out."
            hint = "Second factors are unchanged."

        self.stdout.write(self.style.SUCCESS(title))
        if not options["password_stdin"]:
            self.stdout.write("")
            self.stdout.write(f"    Password: {password}")
            self.stdout.write("")
            self.stdout.write(
                "It is shown only now. Change it after signing in (Settings → Security → Password)."
            )
        self.stdout.write(hint)
