import json
from pathlib import Path

import pyotp
import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import Client

from apps.accounts.models import User, UserSession
from apps.processing.models import Job
from apps.transfer import access, api, archive, state
from docnest import instance_key

from .conftest import PASSWORD

J = "application/json"
EXPORT_PASSWORD = "Export-Passphrase-2026!"


def instance_files(data: Path) -> None:
    (data / "intake").mkdir(exist_ok=True)
    (data / "intake" / "waiting.pdf.enc").write_bytes(b"encrypted intake")
    (data / "intake" / "scans" / "s1").mkdir(parents=True)
    (data / "intake" / "scans" / "s1" / "page-1.enc").write_bytes(b"page")
    (data / "view-cache").mkdir(exist_ok=True)
    (data / "view-cache" / "cached.enc").write_bytes(b"cache")
    (data / "work" / "tmp-plaintext").write_bytes(b"plain")
    (data / "intake" / "half.pdf.enc.partial").write_bytes(b"half")
    (data / "proton" / "session").mkdir(parents=True, exist_ok=True)
    (data / "proton" / "session" / "auth-session.enc").write_bytes(b"proton session")
    (data / "proton" / "session" / ".cli.lock").write_bytes(b"")


def members(path: Path) -> dict[str, bytes]:
    import pyzipper

    with pyzipper.AESZipFile(path) as zf:
        zf.setpassword(EXPORT_PASSWORD.encode())
        return {name: zf.read(name) for name in zf.namelist()}


@pytest.mark.django_db
def test_export_holds_database_key_and_files_but_no_caches(isolated_dirs, user):
    instance_files(isolated_dirs)
    dest = isolated_dirs / "out.zip"

    with state.exclusive("export"):
        manifest = archive.export(dest, EXPORT_PASSWORD)

    content = members(dest)
    assert content["master_key"].decode() == settings.MASTER_KEY
    assert content["data/intake/waiting.pdf.enc"] == b"encrypted intake"
    assert content["data/intake/scans/s1/page-1.enc"] == b"page"
    assert content["proton-session/auth-session.enc"] == b"proton session"
    assert "data/proton/session/auth-session.enc" not in content  # its own root, not twice
    assert "proton-session/.cli.lock" not in content
    assert not [n for n in content if "view-cache" in n or "work/" in n or n.endswith(".partial")]
    saved = json.loads(content["manifest.json"])
    assert saved["counts"] == {"users": 1, "documents": 0} == manifest["counts"]
    assert "0001_initial" in saved["migrations"]["accounts"]
    assert all(not name.endswith(("_FILE", "_KEY", "PASSWORD")) for name in saved["config"])
    assert state.read_status()["state"] == "done"
    assert not state.paused()


@pytest.mark.django_db
def test_inspect_checks_password_and_version(isolated_dirs, user):
    dest = isolated_dirs / "out.zip"
    archive.export(dest, EXPORT_PASSWORD)

    assert archive.inspect(dest, EXPORT_PASSWORD)["counts"]["users"] == 1
    with pytest.raises(state.TransferError, match="Wrong password"):
        archive.inspect(dest, "not the password")
    not_zip = isolated_dirs / "notes.zip"
    not_zip.write_text("hello")
    with pytest.raises(state.TransferError, match="not a DocNest export"):
        archive.inspect(not_zip, EXPORT_PASSWORD)


@pytest.mark.django_db
def test_export_from_a_newer_version_is_refused(isolated_dirs, user, monkeypatch):
    dest = isolated_dirs / "out.zip"
    real = archive._manifest

    def newer():
        manifest = real()
        manifest["migrations"]["documents"].append("9999_from_the_future")
        return manifest

    monkeypatch.setattr(archive, "_manifest", newer)
    archive.export(dest, EXPORT_PASSWORD)
    with pytest.raises(state.TransferError, match="newer DocNest"):
        archive.inspect(dest, EXPORT_PASSWORD)


@pytest.mark.django_db
def test_warnings_name_settings_to_copy(isolated_dirs, user, monkeypatch, settings):
    monkeypatch.setenv("DOCNEST_OCR_LANGUAGES", "deu+eng+fra")
    dest = isolated_dirs / "out.zip"
    archive.export(dest, EXPORT_PASSWORD)
    monkeypatch.setenv("DOCNEST_OCR_LANGUAGES", "deu+eng")
    settings.BASE_URL = "https://new.example.com"

    notes = archive.inspect(dest, EXPORT_PASSWORD)["warnings"]
    assert any("DOCNEST_OCR_LANGUAGES=deu+eng+fra (here: deu+eng)" in n for n in notes)
    assert not any("Passkeys" in n for n in notes)  # this server's accounts are kept
    user.delete()  # a new server takes over the accounts, passkeys registered for the old address
    assert any("Passkeys" in n for n in archive.inspect(dest, EXPORT_PASSWORD)["warnings"])


@pytest.mark.django_db(transaction=True)
def test_import_replaces_the_data_and_keeps_this_servers_accounts(isolated_dirs, user, monkeypatch):
    from apps.taxonomy.models import Folder

    instance_files(isolated_dirs)
    Folder.objects.create(parent=None, name="From the old server")
    dest = isolated_dirs / "transfer" / "export.zip"
    state.directory()
    archive.export(dest, EXPORT_PASSWORD)

    # The new server: its own user, signed in. The data changes are undone by the import.
    user.delete()
    Folder.objects.filter(name="From the old server").delete()
    me = User.objects.create(username="me")
    session = Session.objects.create(session_key="x" * 32, session_data="", expire_date="2099-01-01T00:00Z")
    UserSession.objects.create(user=me, session_key=session.session_key)
    (isolated_dirs / "intake" / "waiting.pdf.enc").unlink()
    (isolated_dirs / "intake" / "later.pdf.enc").write_bytes(b"later")
    (isolated_dirs / "proton" / "session" / "auth-session.enc").write_bytes(b"other account")

    with state.exclusive("import"):
        notes = archive.restore(dest, EXPORT_PASSWORD)

    assert any("accounts of this server were kept" in n for n in notes)
    assert list(User.objects.values_list("username", flat=True)) == ["me"]
    assert Session.objects.filter(session_key="x" * 32).exists()  # still signed in
    assert UserSession.objects.get().user_id == me.pk
    assert User.objects.create(username="later").pk > me.pk  # sequences continue after kept rows
    assert Folder.objects.filter(name="From the old server").exists()
    assert state.read_status()["accounts_kept"] is True
    assert (isolated_dirs / "intake" / "waiting.pdf.enc").read_bytes() == b"encrypted intake"
    assert not (isolated_dirs / "intake" / "later.pdf.enc").exists()
    assert (isolated_dirs / "proton" / "session" / "auth-session.enc").read_bytes() == b"proton session"
    assert not (isolated_dirs / "view-cache").exists()  # cache of the replaced instance
    assert (isolated_dirs / "work" / "tmp-plaintext").exists()  # this server's own work area
    assert dest.exists()  # the transfer folder is never replaced
    assert not (isolated_dirs / archive.STAGING).exists()
    assert not instance_key.path(isolated_dirs).exists()  # same master key: nothing to store


@pytest.mark.django_db(transaction=True)
def test_new_server_takes_over_the_accounts_of_the_export(isolated_dirs, user):
    dest = isolated_dirs / "transfer" / "export.zip"
    state.directory()
    archive.export(dest, EXPORT_PASSWORD)
    user.delete()

    notes = archive.restore(dest, EXPORT_PASSWORD)

    assert list(User.objects.values_list("username", flat=True)) == ["alice"]
    assert not any("kept" in n for n in notes)


@pytest.mark.django_db(transaction=True)
def test_kept_second_factor_works_with_the_imported_master_key(isolated_dirs, user, monkeypatch, settings):
    from apps.accounts import services
    from apps.accounts.models import RecoveryCode, TotpDevice
    from apps.crypto import keys
    from apps.transfer import accounts

    dest = isolated_dirs / "transfer" / "export.zip"
    state.directory()
    archive.export(dest, EXPORT_PASSWORD)
    monkeypatch.setattr(archive, "_migrate", lambda: None)  # a new process would read the real secret

    # The new server has its own master key, a user with an authenticator app and recovery codes.
    user.delete()
    with accounts.master_key("the-new-servers-own-master-key-0123456789abc"):
        me = User.objects.create(username="me")
        device, secret = services.create_totp_device(me)
        device.confirmed = True
        device.save()
        services.generate_recovery_codes(me)
        notes = archive.restore(dest, EXPORT_PASSWORD)

    keys.derive.cache_clear()
    totp = TotpDevice.objects.get(user=me)
    assert totp.confirmed
    assert services.verify_totp(totp, pyotp.TOTP(secret).now())
    assert not RecoveryCode.objects.exists()
    assert any("Recovery codes" in n for n in notes)


@pytest.mark.django_db(transaction=True)
def test_failed_database_restore_changes_nothing(isolated_dirs, user, monkeypatch):
    instance_files(isolated_dirs)
    dest = isolated_dirs / "transfer" / "export.zip"
    state.directory()
    archive.export(dest, EXPORT_PASSWORD)
    User.objects.create(username="bob")
    (isolated_dirs / "intake" / "later.pdf.enc").write_bytes(b"later")

    monkeypatch.setattr(
        archive.subprocess,
        "run",
        lambda cmd, **kw: archive.subprocess.CompletedProcess(cmd, 1 if "--file=-" in cmd else 0),
    )
    with pytest.raises(state.TransferError, match="nothing was changed"):
        archive.restore(dest, EXPORT_PASSWORD)

    assert set(User.objects.values_list("username", flat=True)) == {"alice", "bob"}
    assert (isolated_dirs / "intake" / "later.pdf.enc").exists()
    assert not (isolated_dirs / archive.STAGING).exists()


def test_imported_master_key_replaces_the_local_one(tmp_path):
    local, imported = "l" * 40, "i" * 40
    instance_key.store(local, imported, tmp_path)

    assert instance_key.effective(local, tmp_path) == imported
    with pytest.raises(instance_key.InstanceKeyError):
        instance_key.effective("x" * 40, tmp_path)
    instance_key.store(local, local, tmp_path)
    assert not instance_key.path(tmp_path).exists()
    assert instance_key.effective(local, tmp_path) == local


# --- API -------------------------------------------------------------------------


@pytest.fixture
def spawned(monkeypatch):
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(api, "_spawn", lambda args, password: calls.append((args, password)))
    return calls


def reauth(client: Client) -> None:
    assert client.post("/api/v1/auth/reauth", {"password": PASSWORD}, content_type=J).status_code == 200


def test_export_needs_a_strong_password_and_recent_sign_in(api, spawned):
    reauth(api)
    r = api.post("/api/v1/transfer/export", {"password": "short"}, content_type=J)
    assert r.status_code == 422

    r = api.post("/api/v1/transfer/export", {"password": EXPORT_PASSWORD}, content_type=J)
    assert r.status_code == 200, r.content
    assert r.json()["operation"] == "export" and r.json()["state"] == "running"
    [(args, password)] = spawned
    assert args[0] == "export_instance" and password == EXPORT_PASSWORD


def test_finished_export_can_be_downloaded(api, isolated_dirs, user):
    name = archive.export_name()
    with state.exclusive("export"):
        archive.export(state.directory() / name, EXPORT_PASSWORD)
        state.write_status(export={"name": name})

    status = api.get("/api/v1/transfer/status").json()
    assert status["export"]["name"] == name
    r = api.get("/api/v1/transfer/export/download")
    assert r.status_code == 200
    assert b"".join(r.streaming_content)[:2] == b"PK"
    assert Client().get("/api/v1/transfer/export/download").status_code == 401
    assert Client().get("/api/v1/transfer/status").json()["export"] is None


@pytest.mark.django_db
def test_new_instance_imports_with_the_setup_code(isolated_dirs, user, spawned):
    archive.export(isolated_dirs / "export.zip", EXPORT_PASSWORD)
    body = (isolated_dirs / "export.zip").read_bytes()
    user.delete()  # a new instance: no users
    anonymous = Client()
    assert anonymous.get("/api/v1/transfer/status").json()["instance_empty"] is True

    url = "/api/v1/transfer/import/upload"
    r = anonymous.post(url, body, content_type="application/octet-stream")
    assert r.status_code == 403
    r = anonymous.post(
        url, body, content_type="application/octet-stream", HTTP_X_DOCNEST_SETUP_CODE=access.setup_code()
    )
    assert r.status_code == 200 and r.json()["size"] == len(body)

    code = access.setup_code()
    r = anonymous.post(
        "/api/v1/transfer/import/inspect", {"password": "wrong", "setup_code": code}, content_type=J
    )
    assert r.status_code == 422 and "Wrong password" in r.json()["detail"]
    r = anonymous.post(
        "/api/v1/transfer/import/inspect", {"password": EXPORT_PASSWORD, "setup_code": code}, content_type=J
    )
    assert r.status_code == 200 and r.json()["users"] == 1

    r = anonymous.post(
        "/api/v1/transfer/import/start", {"password": EXPORT_PASSWORD, "setup_code": code}, content_type=J
    )
    assert r.status_code == 200
    [(args, password)] = spawned
    assert args[:2] == ["import_instance", str(state.directory() / "import.zip")]


def test_instance_with_users_needs_a_signed_in_user_to_import(user):
    r = Client().post(
        "/api/v1/transfer/import/upload",
        b"PK",
        content_type="application/octet-stream",
        HTTP_X_DOCNEST_SETUP_CODE=access.setup_code(),
    )
    assert r.status_code == 401


def test_api_pauses_while_an_import_runs(api, settings):
    with state.exclusive("import"):
        assert api.get("/api/v1/overview").status_code == 503
        status = api.get("/api/v1/transfer/status")
        assert status.status_code == 200 and status.json()["running"] is True
        # The import replaces the session table: following it must not end the browser's session.
        kept = list(Session.objects.all())
        Session.objects.all().delete()
        status = api.get("/api/v1/transfer/status")
        assert status.status_code == 200
        Session.objects.bulk_create(kept)  # the import puts this server's sessions back
    assert api.get("/api/v1/overview").status_code == 200


def test_export_waits_for_running_jobs(db, monkeypatch):
    from django.utils import timezone

    Job.objects.create(
        kind=Job.Kind.TRAIN_CLASSIFIER,
        state=Job.State.RUNNING,
        locked_until=timezone.now() + timezone.timedelta(hours=1),
    )
    monkeypatch.setattr(state, "JOB_WAIT_SECONDS", 0)
    with pytest.raises(state.TransferError, match="1 job is still running"), state.exclusive("export"):
        pass
    assert state.read_status()["state"] == "failed"
    assert not state.paused()


@pytest.mark.django_db
def test_import_code_command_prints_the_code_only_on_a_new_instance(capsys, user):
    call_command("import_code")
    assert access.setup_code() not in capsys.readouterr().out
    user.delete()
    call_command("import_code")
    assert access.setup_code() in capsys.readouterr().out


@pytest.mark.django_db(transaction=True)
def test_import_on_a_server_with_another_master_key_keeps_the_exported_one(
    isolated_dirs, user, monkeypatch, settings
):
    dest = isolated_dirs / "transfer" / "export.zip"
    state.directory()
    archive.export(dest, EXPORT_PASSWORD)
    settings.LOCAL_MASTER_KEY = "a-new-server-has-its-own-master-key-0123456789"
    monkeypatch.setattr(archive, "_migrate", lambda: None)  # a new process would read the real secret

    archive.restore(dest, EXPORT_PASSWORD)

    assert instance_key.effective(settings.LOCAL_MASTER_KEY, isolated_dirs) == settings.MASTER_KEY
