"""Authenticated encryption helpers (AES-256-GCM).

Small values (text, JSON, thumbnails) are encrypted in one piece with
`encrypt_bytes`. Files are encrypted in chunks with `encrypt_file` so they can
be processed without loading them into memory; every chunk is authenticated
together with its index and a final-chunk flag, which prevents truncation and
reordering.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.crypto.keys import KEY_VERSION, Purpose, derive

NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
FILE_MAGIC = b"DNEF"  # DocNest Encrypted File
FILE_FORMAT = 1


class DecryptionError(Exception):
    pass


def encrypt_bytes(plaintext: bytes, *, purpose: str = Purpose.CONTENT, aad: bytes = b"") -> bytes:
    """Return version(1) || nonce(12) || ciphertext+tag."""
    nonce = os.urandom(NONCE_SIZE)
    ct = AESGCM(derive(purpose, KEY_VERSION)).encrypt(nonce, plaintext, aad)
    return bytes([KEY_VERSION]) + nonce + ct


def decrypt_bytes(blob: bytes, *, purpose: str = Purpose.CONTENT, aad: bytes = b"") -> bytes:
    if len(blob) < 1 + NONCE_SIZE + TAG_SIZE:
        raise DecryptionError("ciphertext too short")
    version, nonce, ct = blob[0], blob[1 : 1 + NONCE_SIZE], blob[1 + NONCE_SIZE :]
    try:
        return AESGCM(derive(purpose, version)).decrypt(nonce, ct, aad)
    except Exception as exc:
        raise DecryptionError("cannot decrypt value") from exc


def encrypt_text(text: str, *, aad: bytes = b"") -> bytes:
    return encrypt_bytes(text.encode("utf-8"), aad=aad)


def decrypt_text(blob: bytes | memoryview | None, *, aad: bytes = b"") -> str:
    if blob is None:
        return ""
    return decrypt_bytes(bytes(blob), aad=aad).decode("utf-8")


# --- Files ------------------------------------------------------------------

_HEADER = struct.Struct(">4sBB16s")  # magic, format, key version, per-file salt


def _file_key(version: int, salt: bytes) -> AESGCM:
    # A fresh key per file (HKDF with a random salt) lets chunk nonces be a
    # simple counter without any risk of nonce reuse across files.
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"docnest/file-chunk").derive(
        derive(Purpose.FILES, version)
    )
    return AESGCM(key)


def _chunk_nonce(index: int) -> bytes:
    return b"\x00\x00\x00\x00" + index.to_bytes(8, "big")


def _chunk_aad(header: bytes, index: int, final: bool, aad: bytes) -> bytes:
    return header + index.to_bytes(8, "big") + (b"\x01" if final else b"\x00") + aad


def encrypt_stream(src: BinaryIO, dst: BinaryIO, *, aad: bytes = b"") -> int:
    """Encrypt `src` into `dst`. Returns the number of plaintext bytes."""
    salt = os.urandom(16)
    header = _HEADER.pack(FILE_MAGIC, FILE_FORMAT, KEY_VERSION, salt)
    dst.write(header)
    aes = _file_key(KEY_VERSION, salt)
    total = 0
    index = 0
    chunk = src.read(CHUNK_SIZE)
    while True:
        nxt = src.read(CHUNK_SIZE) if chunk else b""
        final = not nxt
        ct = aes.encrypt(_chunk_nonce(index), chunk, _chunk_aad(header, index, final, aad))
        dst.write(struct.pack(">I", len(ct)))
        dst.write(ct)
        total += len(chunk)
        index += 1
        if final:
            break
        chunk = nxt
    return total


def decrypt_stream_chunks(src: BinaryIO, *, aad: bytes = b"") -> Iterator[bytes]:
    header = src.read(_HEADER.size)
    if len(header) != _HEADER.size:
        raise DecryptionError("truncated header")
    magic, fmt, version, salt = _HEADER.unpack(header)
    if magic != FILE_MAGIC or fmt != FILE_FORMAT:
        raise DecryptionError("not a DocNest encrypted file")
    aes = _file_key(version, salt)
    index = 0
    while True:
        raw_len = src.read(4)
        if len(raw_len) != 4:
            raise DecryptionError("truncated file (missing final chunk)")
        (length,) = struct.unpack(">I", raw_len)
        if length > CHUNK_SIZE + TAG_SIZE:
            raise DecryptionError("invalid chunk length")
        ct = src.read(length)
        if len(ct) != length:
            raise DecryptionError("truncated chunk")
        nonce = _chunk_nonce(index)
        for final in (False, True):
            try:
                pt = aes.decrypt(nonce, ct, _chunk_aad(header, index, final, aad))
            except Exception:  # noqa: S112 - try the other final-flag value
                continue
            yield pt
            if final:
                if src.read(1):
                    raise DecryptionError("trailing data after final chunk")
                return
            break
        else:
            raise DecryptionError("chunk authentication failed")
        index += 1


def encrypt_file(src: Path, dst: Path, *, aad: bytes = b"") -> int:
    tmp = dst.with_name(dst.name + ".partial")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            size = encrypt_stream(fin, fout, aad=aad)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dst)
        _fsync_dir(dst.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return size


def decrypt_file(src: Path, dst: Path, *, aad: bytes = b"") -> None:
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            for chunk in decrypt_stream_chunks(fin, aad=aad):
                fout.write(chunk)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


def _fsync_dir(path: Path) -> None:
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
