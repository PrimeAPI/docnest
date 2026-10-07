"""Persistent, on-demand Docling model cache management."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import os
import subprocess
from pathlib import Path

from django.conf import settings

VLM_REPO_ID = "numind/NuExtract-2.0-2B"
VLM_REVISION = "fe5b2f0b63b81150721435a3ca1129a75c59c74e"
VLM_DIRECTORY = "numind--NuExtract-2.0-2B"
STANDARD_DIRECTORIES = (
    "docling-project--docling-layout-heron",
    "docling-project--docling-models",
)


class ModelDownloadFailed(RuntimeError):
    pass


def ensure_models(*, include_vlm: bool = False, force: bool = False) -> Path:
    """Download the compatible model set into the persistent cache when needed.

    A filesystem lock prevents multiple web/worker containers sharing the
    volume from downloading the same multi-gigabyte files concurrently. Failed
    Hugging Face downloads remain resumable and never receive a ready marker.
    """
    root = Path(settings.DOCLING_ARTIFACTS_PATH)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_file = (root / ".download.lock").open("a+b")
    except OSError as exc:
        raise ModelDownloadFailed(f"Docling model cache is not writable: {root}") from exc

    with lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _ensure_standard(root, force=force)
            if include_vlm:
                _ensure_vlm(root, force=force)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    return root


def _marker(root: Path, kind: str, identity: str) -> Path:
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return root / f".docnest-{kind}-{digest}.ready"


def _ready(marker: Path, required: tuple[Path, ...]) -> bool:
    return marker.is_file() and all(path.exists() for path in required)


def _write_marker(marker: Path, identity: str) -> None:
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(identity + "\n", encoding="utf-8")
    os.replace(temporary, marker)


def _ensure_standard(root: Path, *, force: bool) -> None:
    version = importlib.metadata.version("docling-slim")
    identity = f"docling-slim={version};models=layout,tableformer"
    marker = _marker(root, "standard", identity)
    required = tuple(root / name for name in STANDARD_DIRECTORIES)
    if not force and _ready(marker, required):
        return
    command = [
        "docling-tools",
        "models",
        "download",
        "--output-dir",
        str(root),
    ]
    if force:
        command.append("--force")
    command.extend(["layout", "tableformer"])
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ModelDownloadFailed("Could not download the standard Docling models") from exc
    if not all(path.exists() for path in required):
        raise ModelDownloadFailed("The standard Docling model download is incomplete")
    _write_marker(marker, identity)


def _ensure_vlm(root: Path, *, force: bool) -> None:
    identity = f"repo={VLM_REPO_ID};revision={VLM_REVISION}"
    marker = _marker(root, "vlm", identity)
    model_dir = root / VLM_DIRECTORY
    required = (model_dir / "config.json",)
    if not force and _ready(marker, required):
        return
    try:
        from docling.models.extraction.transformers_extraction_model import (
            TransformersExtractionModel,
        )

        TransformersExtractionModel.download_models(
            repo_id=VLM_REPO_ID,
            revision=VLM_REVISION,
            local_dir=model_dir,
            force=force,
            progress=True,
        )
    except Exception as exc:
        raise ModelDownloadFailed("Could not download the Docling VLM extraction model") from exc
    if not all(path.exists() for path in required):
        raise ModelDownloadFailed("The Docling VLM model download is incomplete")
    _write_marker(marker, identity)
