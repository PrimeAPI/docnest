from __future__ import annotations

import time
from pathlib import Path

import pyotp
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts import services
from apps.accounts.models import User
from apps.scanners import tokens
from apps.scanners.models import ScannerClient

PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture(autouse=True)
def isolated_dirs(settings, tmp_path: Path):
    settings.DATA_DIR = tmp_path
    settings.INTAKE_DIR = tmp_path / "intake"
    settings.WORK_DIR = tmp_path / "work"
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path / "work" / "uploads")
    settings.STORAGE_LOCAL_DIR = tmp_path / "storage"
    settings.VIEW_CACHE_DIR = tmp_path / "view-cache"
    settings.STORAGE_BACKEND = "local"
    for d in ("intake", "work/uploads", "storage"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def user(db) -> User:
    u = User(username="alice")
    u.set_password(PASSWORD)
    u.save()
    return u


@pytest.fixture
def totp_user(user: User) -> tuple[User, str]:
    device, secret = services.create_totp_device(user)
    device.confirmed = True
    device.save()
    return user, secret


def totp_code(secret: str, offset_steps: int = 0) -> str:
    totp = pyotp.TOTP(secret)
    return totp.at(time.time() + offset_steps * totp.interval)


@pytest.fixture
def api(totp_user) -> Client:
    """A client with a fully authenticated (password + TOTP) session."""
    user, secret = totp_user
    client = Client()
    r = client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    r = client.post("/api/v1/auth/mfa/totp", {"code": totp_code(secret)}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["mfa_complete"] is True
    return client


@pytest.fixture
def scanner(db) -> tuple[ScannerClient, str]:
    token, prefix, token_hash = tokens.generate()
    client = ScannerClient.objects.create(
        name="Desk scanner",
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=[ScannerClient.Scope.UPLOAD, ScannerClient.Scope.UPLOAD_STATUS],
    )
    return client, token


def upload(client: Client, token: str, pdf: bytes, **fields: str):
    data = {
        "bucket": "private",
        **fields,
        "file": SimpleUploadedFile("scan.pdf", pdf, content_type="application/pdf"),
    }
    headers = {}
    if "idempotency_key" in fields:
        headers["HTTP_IDEMPOTENCY_KEY"] = data.pop("idempotency_key")
    return client.post("/api/upload/v1/documents", data, HTTP_AUTHORIZATION=f"Bearer {token}", **headers)


@pytest.fixture(autouse=True)
def seed_taxonomy(request):
    """Transactional tests flush the DB (including seed migrations); re-seed."""
    if request.node.get_closest_marker("django_db") is None:
        return
    request.getfixturevalue("db") if "transactional_db" not in request.fixturenames else None
    from apps.taxonomy.models import DocumentType, Folder

    for name in ("Private", "Business", "Studies"):
        Folder.objects.get_or_create(parent=None, name=name)
    for name in ("Mail", "Contract", "Invoice", "Notice", "Statement", "Other"):
        DocumentType.objects.get_or_create(slug=name.lower(), defaults={"name": name})
