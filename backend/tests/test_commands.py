import io
import re

import pytest
from django.core.management import call_command
from django.test import Client

from apps.accounts.models import User, UserSession

pytestmark = pytest.mark.django_db
J = "application/json"


def run(*args: str, stdin: str | None = None) -> str:
    out = io.StringIO()
    if stdin is not None:
        import sys

        old, sys.stdin = sys.stdin, io.StringIO(stdin)
        try:
            call_command("createuser", *args, stdout=out)
        finally:
            sys.stdin = old
    else:
        call_command("createuser", *args, stdout=out)
    return out.getvalue()


def generated(output: str) -> str:
    m = re.search(r"Password: (\S+)", output)
    assert m, output
    return m.group(1)


def test_createuser_generates_a_password_that_works():
    out = run("bob")
    pw = generated(out)
    assert len(pw) >= 20
    assert User.objects.get(username="bob").check_password(pw)
    r = Client().post("/api/v1/auth/login", {"username": "bob", "password": pw}, content_type=J)
    assert r.status_code == 200 and r.json()["needs_enrollment"] is True


def test_existing_user_gets_a_new_password_and_sessions_end(totp_user):
    user, _ = totp_user
    UserSession.objects.create(user=user, session_key="x" * 32)
    old_hash = user.password
    out = run("ALICE")  # case-insensitive match
    pw = generated(out)
    user.refresh_from_db()
    assert user.password != old_hash and user.check_password(pw)
    assert "reset" in out
    assert not UserSession.objects.filter(user=user).exists()
    assert user.totp_devices.filter(confirmed=True).exists()  # second factor kept
    assert User.objects.count() == 1


def test_password_stdin_is_validated():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        run("carol", "--password-stdin", stdin="short\n")
    out = run("carol", "--password-stdin", stdin="A-Valid-Passw0rd-Here\n")
    assert "Password:" not in out
