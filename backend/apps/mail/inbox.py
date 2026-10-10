"""Import documents from an email inbox over IMAP.

The user forwards an email to an inbox set up for DocNest. Every few minutes the
worker looks into it: for each message from an allowed sender, the email itself
is stored as a PDF document and every attached PDF or picture becomes a document
of its own; all of them go through the normal processing, with the email as
context for the AI model, but are never filed into a folder. Imported messages
are deleted from the inbox. Messages from other senders are left untouched and
are never imported.
"""

from __future__ import annotations

import contextlib
import email.utils
import hashlib
import hmac
import imaplib
import io
import logging
import re
import shutil
import ssl
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from apps.audit.service import audit
from apps.crypto.aead import decrypt_text, encrypt_text
from apps.crypto.keys import Purpose, derive
from apps.documents.intake import IntakeError, IntakeRequest, receive
from apps.documents.models import Document
from apps.mail import config, render
from apps.mail.models import MailMessage
from apps.processing import queue
from apps.processing.models import Job

logger = logging.getLogger(__name__)

TIMEOUT = 60
MAX_MESSAGES_PER_RUN = 50
MIN_IMAGE_EDGE = 400  # smaller pictures are logos and signatures, not documents
CONTEXT_CHARS = 4000


class MailError(Exception):
    """The inbox could not be reached, logged in to or read."""


# --- Parsing --------------------------------------------------------------------------------


@dataclass
class Attachment:
    name: str
    data: bytes


@dataclass
class ParsedEmail:
    message_key: str
    view: render.EmailView
    attachments: list[Attachment] = field(default_factory=list)


def message_key(msg: Message, raw: bytes) -> str:
    ident = (msg.get("message-id") or "").strip().encode() or hashlib.sha256(raw).digest()
    return hmac.new(derive(Purpose.TOKENS), b"mail:" + ident, hashlib.sha256).hexdigest()


def sender_address(msg: Message) -> str:
    return email.utils.parseaddr(str(msg.get("from") or ""))[1].casefold()


def _text_of(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, ValueError):  # unknown charset or broken encoding
        payload = body.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if body.get_content_subtype() == "html":
        return render.html_to_text(str(content))
    return render.tidy(str(content))


def _headers(msg: Message) -> str:
    lines = [f"{name}: {msg.get(name)}" for name in ("From", "Date", "Subject") if msg.get(name)]
    return "\n".join(lines)


def _is_document(data: bytes) -> bool:
    """PDFs, and pictures big enough to be a photographed or scanned page."""
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return True
    try:
        with Image.open(io.BytesIO(data)) as image:
            return min(image.size) >= MIN_IMAGE_EDGE
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return False


def parse(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    text = _text_of(msg)
    # A message forwarded "as attachment": its headers and text are the real context.
    for part in msg.walk():
        if part is not msg and part.get_content_type() == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            if isinstance(inner, EmailMessage):
                text += (
                    "\n\n---------- Forwarded message ----------\n"
                    + _headers(inner)
                    + "\n\n"
                    + _text_of(inner)
                )

    attachments: list[Attachment] = []
    names: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() == "message":
            continue
        name = part.get_filename()
        disposition = part.get_content_disposition()
        if not name and disposition != "attachment":
            continue  # body text or an unnamed inline part
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        name = Path(name or "attachment").name[:150]
        if _is_document(payload):
            attachments.append(Attachment(name, payload))
            names.append(name)
        elif part.get_content_maintype() != "image":
            names.append(f"{name} (not imported: not a PDF or picture)")

    sent = None
    with contextlib.suppress(TypeError, ValueError):
        sent = email.utils.parsedate_to_datetime(str(msg.get("date"))) if msg.get("date") else None
    view = render.EmailView(
        subject=str(msg.get("subject") or "").strip()[:300],
        sender=str(msg.get("from") or "").strip()[:300],
        to=str(msg.get("to") or "").strip()[:300],
        sent_at=sent,
        text=render.tidy(text),
        attachments=names,
    )
    return ParsedEmail(message_key(msg, raw), view, attachments)


# --- Importing ----------------------------------------------------------------------------------


def context_text(mail: MailMessage) -> str:
    """What the AI model is told about the email a document came with."""
    parts = [
        f"Subject: {decrypt_text(mail.subject_enc)}",
        f"From: {decrypt_text(mail.sender_enc)}",
        "",
        decrypt_text(mail.text_enc),
    ]
    return "\n".join(parts).strip()[:CONTEXT_CHARS]


def import_message(raw: bytes) -> tuple[MailMessage, int]:
    """Store the email and its attachments as documents. Returns the message and the documents created."""
    parsed = parse(raw)
    view = parsed.view
    mail = MailMessage.objects.filter(message_key=parsed.message_key).first()
    if mail is not None and mail.imported_at is not None:
        return mail, 0  # imported before; deleting it from the inbox had failed
    if mail is None:
        # An interrupted import is resumed: the same files are recognised as duplicates.
        mail = MailMessage.objects.create(
            message_key=parsed.message_key,
            subject_enc=encrypt_text(view.subject),
            sender_enc=encrypt_text(view.sender),
            text_enc=encrypt_text(view.text),
            sent_at=view.sent_at,
        )
    created = 0
    work = Path(tempfile.mkdtemp(prefix="mail-", dir=settings.WORK_DIR))
    try:
        pdf = work / "email.pdf"
        render.to_pdf(view, pdf)
        subject = re.sub(r"[\\/:*?\"<>|]+", " ", view.subject).strip()[:120] or "E-Mail"
        result = receive(pdf, IntakeRequest(filename=f"{subject}.pdf", mail_id=mail.pk))
        created += result.created
        mail.email_document = result.document
        mail.save(update_fields=["email_document"])
        for position, attachment in enumerate(parsed.attachments, start=1):
            path = work / f"attachment-{position}"
            path.write_bytes(attachment.data)
            try:
                result = receive(path, IntakeRequest(filename=attachment.name, mail_id=mail.pk))
            except IntakeError as exc:
                logger.warning("email attachment not imported", extra={"error": str(exc)})
                continue
            created += result.created
    finally:
        shutil.rmtree(work, ignore_errors=True)
    mail.imported_at = timezone.now()
    mail.save(update_fields=["imported_at"])
    return mail, created


# --- IMAP ---------------------------------------------------------------------------------------


def connect(cfg: config.MailSettings) -> imaplib.IMAP4:
    context = ssl.create_default_context()
    if not cfg.verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        conn: imaplib.IMAP4
        if cfg.security == "ssl":
            conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=context, timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=TIMEOUT)
            conn.starttls(ssl_context=context)
        conn.login(cfg.username, cfg.password)
        typ, _ = conn.select(_quote(cfg.folder))
        if typ != "OK":
            raise MailError(f"The folder “{cfg.folder}” does not exist")
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(_describe(exc)) from exc
    return conn


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _describe(exc: Exception) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return (
            f"The server's certificate is not trusted ({exc.verify_message}). "
            "For a local bridge, turn off the certificate check."
        )
    if isinstance(exc, imaplib.IMAP4.error):
        message = str(exc)
        return f"The server refused: {message}" if message else "The server refused the login"
    if isinstance(exc, TimeoutError):
        return "The server did not answer in time"
    return f"Could not connect: {exc}"


def test(cfg: config.MailSettings) -> int:
    """Log in and count the messages waiting. Raises MailError."""
    conn = connect(cfg)
    try:
        typ, data = conn.uid("SEARCH", "UNDELETED")
        return len(data[0].split()) if typ == "OK" and data and data[0] else 0
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _fetch_part(conn: imaplib.IMAP4, uid: bytes, what: str) -> bytes:
    typ, data = conn.uid("FETCH", uid.decode(), what)
    if typ != "OK":
        raise MailError(f"Could not read message {uid.decode()}")
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            return bytes(item[1])
    return b""


def _size(conn: imaplib.IMAP4, uid: bytes) -> int:
    typ, data = conn.uid("FETCH", uid.decode(), "(RFC822.SIZE)")
    match = re.search(
        rb"RFC822\.SIZE (\d+)", data[0] if typ == "OK" and data and isinstance(data[0], bytes) else b""
    )
    return int(match.group(1)) if match else 0


def fetch() -> None:
    """Import waiting messages from allowed senders and delete them from the inbox."""
    cfg = config.load()
    if not cfg.ready():
        return
    imported = ignored = documents = 0
    problems: list[str] = []
    try:
        conn = connect(cfg)
    except MailError as exc:
        config.set_status(checked_at=timezone.now().isoformat(), ok=False, message=str(exc))
        logger.warning("email inbox not reachable", extra={"error": str(exc)})
        return
    try:
        typ, data = conn.uid("SEARCH", "UNDELETED")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        for uid in uids[:MAX_MESSAGES_PER_RUN]:
            header = _fetch_part(conn, uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            sender = sender_address(BytesParser(policy=policy.default).parsebytes(header, headersonly=True))
            if not cfg.allows(sender):
                ignored += 1
                continue
            if _size(conn, uid) > settings.MAX_SCAN_BYTES:
                problems.append(f"A message from {sender} is too large to import")
                continue
            try:
                _mail, created = import_message(_fetch_part(conn, uid, "(BODY.PEEK[])"))
            except Exception as exc:
                logger.exception("email could not be imported")
                problems.append(f"A message from {sender} could not be imported: {type(exc).__name__}")
                continue
            conn.uid("STORE", uid.decode(), "+FLAGS.SILENT", "(\\Deleted)")
            imported += 1
            documents += created
            audit("mail.imported", target=sender, documents=created)
        if imported:
            conn.expunge()
    except (imaplib.IMAP4.error, OSError) as exc:
        problems.append(_describe(exc))
    finally:
        with contextlib.suppress(Exception):
            conn.logout()
    now = timezone.now().isoformat()
    total = config.status()
    config.set_status(
        checked_at=now,
        ok=not problems,
        message="; ".join(problems)[:500],
        ignored=ignored,
        imported_total=int(str(total.get("imported_total") or 0)) + imported,
        **({"last_import_at": now, "last_documents": documents} if imported else {}),
    )


def is_due() -> bool:
    cfg = config.load()
    if not cfg.ready():
        return False
    checked = config.status().get("checked_at")
    if isinstance(checked, str):
        with contextlib.suppress(ValueError):
            last = datetime.fromisoformat(checked)
            if timezone.now() - last < timedelta(minutes=cfg.interval_minutes):
                return False
    return not Job.objects.filter(
        kind=Job.Kind.FETCH_MAIL, state__in=[Job.State.QUEUED, Job.State.RUNNING]
    ).exists()


def schedule() -> Job | None:
    if Job.objects.filter(kind=Job.Kind.FETCH_MAIL, state__in=[Job.State.QUEUED, Job.State.RUNNING]).exists():
        return None
    return queue.enqueue(Job.Kind.FETCH_MAIL, payload={}, priority=5)


def document_mail(document: Document) -> MailMessage | None:
    return MailMessage.objects.filter(pk=document.mail_id).first() if document.mail_id else None
