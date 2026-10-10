import io
from email.message import EmailMessage

import pikepdf
import pytest
from PIL import Image

from apps.analysis import ai
from apps.crypto.aead import decrypt_text
from apps.documents.models import Document
from apps.mail import config, inbox
from apps.mail.models import MailMessage
from apps.processing import pipeline
from apps.processing.models import Job
from apps.processing.preferences import set_ai_model
from apps.processing.worker import Worker
from tests.pdfs import text_pdf

pytestmark = pytest.mark.django_db
J = "application/json"
ME = "me@example.org"


def picture(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "JPEG")
    return out.getvalue()


def forwarded(
    *, sender: str = ME, subject: str = "Fwd: Ihre Rechnung", pdf: bytes | None = None, extra=()
) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Me <{sender}>"
    msg["To"] = "docnest@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 09 Oct 2026 10:00:00 +0200"
    msg["Message-ID"] = f"<{subject}-{sender}@example.org>"
    msg.set_content(
        "---------- Forwarded message ---------\nVon: Stadtwerke Delmenhorst <rechnung@stadtwerke.de>\n"
        "Betreff: Ihre Rechnung\n\nSehr geehrte Kundin, anbei Ihre Jahresabrechnung Strom 2026."
    )
    msg.add_alternative(
        "<p>Sehr geehrte Kundin,</p><script>alert(1)</script><p>anbei Ihre Rechnung.</p>", subtype="html"
    )
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="Rechnung.pdf")
    for data, maintype, subtype, name in extra:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class FakeImap:
    """Just enough IMAP for the inbox: UID SEARCH/FETCH/STORE, EXPUNGE."""

    instances: list["FakeImap"] = []
    messages: dict[int, bytes] = {}

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.deleted: set[int] = set()
        self.logged_in = False
        FakeImap.instances.append(self)

    def login(self, user, password):
        if password != "secret":
            raise inbox.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logged_in = True

    def select(self, folder):
        return ("OK", [b"1"]) if folder == '"INBOX"' else ("NO", [b""])

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        uid = int(args[0])
        if command == "FETCH":
            raw = self.messages[uid]
            if "HEADER.FIELDS" in args[1]:
                header = raw.split(b"\n\n", 1)[0] + b"\n\n"
                return "OK", [(b"1 (BODY[HEADER] {n}", header), b")"]
            if "RFC822.SIZE" in args[1]:
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            return "OK", [(b"1 (BODY[] {n}", raw), b")"]
        if command == "STORE":
            self.deleted.add(uid)
            return "OK", [b""]
        raise AssertionError(command)

    def expunge(self):
        for uid in self.deleted:
            FakeImap.messages.pop(uid, None)
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def imap(monkeypatch):
    FakeImap.instances = []
    FakeImap.messages = {}
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", FakeImap)
    config.save(
        config.MailSettings(
            enabled=True, host="imap.example.org", username="docnest", password="secret", allowed_senders=[ME]
        )
    )
    return FakeImap


def test_forwarded_email_becomes_one_document_and_is_deleted(imap, monkeypatch):
    rendered = []
    to_pdf = inbox.render.to_pdf
    monkeypatch.setattr(
        inbox.render, "to_pdf", lambda view, target: (rendered.append(view), to_pdf(view, target))
    )
    picture_page = picture((1200, 1600))
    imap.messages = {
        1: forwarded(
            pdf=text_pdf(["Jahresabrechnung Strom 2026"], extra_pages=[["Seite 2"]]),
            extra=[(picture_page, "image", "jpeg", "Zähler.jpg"), (b"PK..", "application", "zip", "x.zip")],
        )
    }

    inbox.fetch()

    assert imap.messages == {}  # deleted after import
    mail = MailMessage.objects.get()
    [doc] = Document.objects.filter(mail=mail)  # the attachments and the email, together
    assert mail.email_document == doc
    assert doc.folder is None and not doc.has_paper  # never filed
    assert Job.objects.filter(kind=Job.Kind.INTAKE_DOCUMENT).count() == 1
    # The email's pages say where each attachment is: they come first.
    assert rendered[0].attachments == [
        "Rechnung.pdf (pages 1–2)",
        "Zähler.jpg (page 3)",
        "x.zip (not imported: not a PDF or picture)",
    ]
    pipeline.run(doc.pk, intake_only=True)
    doc.refresh_from_db()
    assert doc.page_count == 4  # two PDF pages, the picture, the email
    assert decrypt_text(mail.subject_enc) == "Fwd: Ihre Rechnung"
    assert "Stadtwerke Delmenhorst" in inbox.context_text(mail)
    assert config.status()["ok"] is True and config.status()["imported_total"] == 1


def test_email_pdf_carries_text_but_no_html_code(imap, isolated_dirs):
    parsed = inbox.parse(forwarded())
    target = isolated_dirs / "email.pdf"
    inbox.render.to_pdf(parsed.view, target)
    with pikepdf.open(target) as pdf:
        assert len(pdf.pages) == 1
    assert "alert" not in parsed.view.text and "Jahresabrechnung Strom" in parsed.view.text


def test_only_allowed_senders_are_imported_and_others_stay(imap):
    imap.messages = {
        1: forwarded(sender="stranger@evil.example", pdf=text_pdf(["Phishing"])),
        2: forwarded(sender="ME@Example.org", subject="Fwd: Brief", pdf=text_pdf(["Brief"])),
    }

    inbox.fetch()

    assert list(imap.messages) == [1]  # the stranger's mail is neither imported nor deleted
    assert MailMessage.objects.count() == 1
    assert config.status()["ignored"] == 1


def test_a_whole_domain_can_be_allowed():
    cfg = config.MailSettings(allowed_senders=["@example.org"])
    assert cfg.allows("anyone@example.org") and not cfg.allows("x@example.org.evil.com")


def test_nothing_is_fetched_without_allowed_senders(imap):
    config.save(config.MailSettings(enabled=True, host="h", username="u", password="secret"))
    imap.messages = {1: forwarded(pdf=text_pdf(["x"]))}
    inbox.fetch()
    assert imap.instances == [] and list(imap.messages) == [1]
    assert not inbox.is_due()


def test_small_pictures_are_skipped_and_other_files_listed(imap):
    raw = forwarded(
        extra=[
            (picture((80, 40)), "image", "png", "logo.png"),
            (picture((1200, 1600)), "image", "jpeg", "Foto.jpg"),
            (b"PK\x03\x04", "application", "zip", "Belege.zip"),
        ]
    )
    parsed = inbox.parse(raw)
    assert [a.name for a in parsed.attachments] == ["Foto.jpg"]
    assert parsed.view.attachments == ["Foto.jpg", "Belege.zip (not imported: not a PDF or picture)"]


def test_a_half_imported_email_is_resumed_not_duplicated(imap):
    raw = forwarded(pdf=text_pdf(["Rechnung"]))
    inbox.import_message(raw)
    mail = MailMessage.objects.get()
    mail.imported_at = None  # as if the worker stopped before finishing
    mail.save()
    _, created = inbox.import_message(raw)
    assert created == 0 and Document.objects.count() == 1
    _, created = inbox.import_message(raw)
    assert created == 0 and MailMessage.objects.count() == 1


def test_wrong_password_is_reported(imap):
    config.save(
        config.MailSettings(enabled=True, host="h", username="u", password="nope", allowed_senders=[ME])
    )
    inbox.fetch()
    assert config.status()["ok"] is False and "refused" in config.status()["message"]


def test_worker_schedules_and_runs_the_check(imap):
    imap.messages = {1: forwarded(pdf=text_pdf(["x"]))}
    assert inbox.is_due()
    inbox.schedule()
    assert inbox.schedule() is None  # one at a time
    Worker().run_until_empty(max_jobs=1)
    assert imap.messages == {} and not inbox.is_due()


def test_the_ai_model_reads_attachments_with_the_email_as_context(imap, settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:4b-instruct")
    seen = []

    def fake(model, **kwargs):
        seen.append(kwargs["context"])
        return ai.ModelFields(sender="Stadtwerke Delmenhorst", title="Jahresabrechnung Strom", model=model)

    monkeypatch.setattr(ai, "analyze", fake)
    imap.messages = {1: forwarded(pdf=text_pdf(["Jahresabrechnung Strom 2026"]))}
    inbox.fetch()
    document = Document.objects.get()
    pipeline.run(document.pk, intake_only=True)
    pipeline.run(document.pk)
    [context] = seen
    assert "Stadtwerke Delmenhorst <rechnung@stadtwerke.de>" in context


def test_settings_api_requires_allowlist_and_hides_password(api):
    base = {
        "enabled": True,
        "host": "imap.example.org",
        "port": 993,
        "security": "ssl",
        "verify_tls": True,
        "username": "docnest",
        "password": "secret",
        "folder": "INBOX",
        "interval_minutes": 5,
        "allowed_senders": [],
    }
    r = api.put("/api/v1/mail/settings", base, content_type=J)
    assert r.status_code == 400 and "allowed sender" in r.json()["detail"]
    r = api.put(
        "/api/v1/mail/settings",
        {**base, "allowed_senders": ["Me@Example.org", "not-an-address"]},
        content_type=J,
    )
    assert r.status_code == 400

    r = api.put("/api/v1/mail/settings", {**base, "allowed_senders": ["Me@Example.org"]}, content_type=J)
    assert r.status_code == 200
    body = r.json()
    assert body["allowed_senders"] == ["me@example.org"] and body["has_password"] is True
    assert "secret" not in r.content.decode()
    # The password is kept when none is sent.
    r = api.put("/api/v1/mail/settings", {**base, "password": None, "allowed_senders": [ME]}, content_type=J)
    assert r.status_code == 200 and config.load().password == "secret"
    assert api.post("/api/v1/mail/check").status_code == 200
    assert Job.objects.filter(kind=Job.Kind.FETCH_MAIL).count() == 1


def test_document_detail_shows_the_email(api, imap):
    imap.messages = {1: forwarded(pdf=text_pdf(["Rechnung"]))}
    inbox.fetch()
    document = Document.objects.get()
    body = api.get(f"/api/v1/documents/{document.uuid}").json()
    assert body["received_from"] == "Email"
    assert body["mail"]["subject"] == "Fwd: Ihre Rechnung"
    assert [d["is_email"] for d in body["mail"]["documents"]] == [True]
