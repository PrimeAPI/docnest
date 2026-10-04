import io
from pathlib import Path

import pytest

from apps.crypto import aead
from apps.crypto.keys import Purpose, derive


def test_subkeys_are_purpose_bound():
    assert derive(Purpose.CONTENT) != derive(Purpose.INDEX)
    assert len(derive(Purpose.FILES)) == 32


def test_bytes_roundtrip_and_aad_binding():
    blob = aead.encrypt_bytes(b"secret letter", aad=b"title:1")
    assert b"secret" not in blob
    assert aead.decrypt_bytes(blob, aad=b"title:1") == b"secret letter"
    with pytest.raises(aead.DecryptionError):
        aead.decrypt_bytes(blob, aad=b"title:2")


def test_nonces_are_unique():
    assert aead.encrypt_bytes(b"x") != aead.encrypt_bytes(b"x")


def test_tampering_is_detected():
    blob = bytearray(aead.encrypt_bytes(b"amount 100 EUR"))
    blob[-1] ^= 1
    with pytest.raises(aead.DecryptionError):
        aead.decrypt_bytes(bytes(blob))


@pytest.mark.parametrize("size", [0, 10, aead.CHUNK_SIZE, aead.CHUNK_SIZE * 2 + 17])
def test_file_roundtrip(tmp_path: Path, size: int):
    data = bytes(i % 251 for i in range(size))
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "a.out"
    src.write_bytes(data)
    assert aead.encrypt_file(src, enc, aad=b"doc") == size
    assert data[:64] not in enc.read_bytes() or size == 0
    aead.decrypt_file(enc, out, aad=b"doc")
    assert out.read_bytes() == data
    assert oct(enc.stat().st_mode & 0o777) == "0o600"


def test_file_truncation_is_detected(tmp_path: Path):
    src, enc = tmp_path / "a", tmp_path / "a.enc"
    src.write_bytes(b"x" * (aead.CHUNK_SIZE * 2 + 5))
    aead.encrypt_file(src, enc)
    raw = enc.read_bytes()
    # drop the final chunk entirely
    first_len = int.from_bytes(raw[22:26], "big")
    truncated = raw[
        : 22 + 4 + first_len + 4 + int.from_bytes(raw[22 + 4 + first_len : 22 + 8 + first_len], "big")
    ]
    with pytest.raises(aead.DecryptionError):
        list(aead.decrypt_stream_chunks(io.BytesIO(truncated)))


def test_file_wrong_aad(tmp_path: Path):
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "o"
    src.write_bytes(b"hello")
    aead.encrypt_file(src, enc, aad=b"one")
    with pytest.raises(aead.DecryptionError):
        aead.decrypt_file(enc, out, aad=b"two")
    assert not out.exists()
