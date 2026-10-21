"""The master key of an instance restored from an export.

Everything DocNest encrypts depends on the master key, and an export carries
the key of the instance it was made on. A new server creates its own master
key (`init-secrets.sh`) and cannot read that data, so an import stores the
exported key in the data volume, encrypted with the server's own master key,
and from then on the exported key is the instance's master key.

`docker/proton/docnest-pass` reads the same file (keep the format in sync).
This module must not import Django: settings use it.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FILE_NAME = "master-key.imported"
HKDF_INFO = b"docnest/imported-master-key/v1"
AAD = b"docnest-imported-master-key"
FORMAT_VERSION = 1


class InstanceKeyError(RuntimeError):
    pass


def path(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def _wrapping_key(local_key: str) -> AESGCM:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(local_key.encode())
    return AESGCM(key)


def effective(local_key: str | None, data_dir: Path) -> str | None:
    """The imported master key if this instance was restored from an export, else `local_key`."""
    file = path(data_dir)
    if not local_key or not file.exists():
        return local_key
    try:
        blob = json.loads(file.read_text())
        if blob.get("v") != FORMAT_VERSION:
            raise ValueError("unsupported format")
        key = _wrapping_key(local_key).decrypt(
            base64.b64decode(blob["nonce"]), base64.b64decode(blob["ct"]), AAD
        )
    except Exception as exc:
        raise InstanceKeyError(
            f"Cannot read {file}: it was written with a different master key (secrets/master_key changed?)"
        ) from exc
    return key.decode()


def store(local_key: str, imported_key: str, data_dir: Path) -> None:
    """Make `imported_key` this instance's master key (or drop the override if it is the local key)."""
    file = path(data_dir)
    if imported_key == local_key:
        file.unlink(missing_ok=True)
        return
    nonce = os.urandom(12)
    ct = _wrapping_key(local_key).encrypt(nonce, imported_key.encode(), AAD)
    data = json.dumps(
        {"v": FORMAT_VERSION, "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode()}
    )
    tmp = file.with_name(file.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, file)
