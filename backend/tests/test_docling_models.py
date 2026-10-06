from __future__ import annotations

import subprocess

import pytest

from apps.processing import docling_models


def test_models_download_once_and_are_reused(settings, tmp_path, monkeypatch):
    settings.DOCLING_ARTIFACTS_PATH = tmp_path / "models"
    standard_calls = []
    vlm_calls = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[0] != "docling-tools":
            return real_run(command, *args, **kwargs)
        standard_calls.append((command, kwargs["check"]))
        for name in docling_models.STANDARD_DIRECTORIES:
            (settings.DOCLING_ARTIFACTS_PATH / name).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0)

    def fake_vlm_download(**kwargs):
        vlm_calls.append(kwargs)
        kwargs["local_dir"].mkdir(parents=True, exist_ok=True)
        (kwargs["local_dir"] / "config.json").write_text("{}")

    monkeypatch.setattr(docling_models.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "docling.models.extraction.transformers_extraction_model.TransformersExtractionModel.download_models",
        fake_vlm_download,
    )

    first = docling_models.ensure_models(include_vlm=True)
    second = docling_models.ensure_models(include_vlm=True)

    assert first == second == settings.DOCLING_ARTIFACTS_PATH
    assert len(standard_calls) == 1
    assert standard_calls[0][0][-2:] == ["layout", "tableformer"]
    assert len(vlm_calls) == 1
    assert vlm_calls[0]["repo_id"] == docling_models.VLM_REPO_ID
    assert vlm_calls[0]["revision"] == docling_models.VLM_REVISION


def test_failed_download_is_retried_instead_of_marked_ready(settings, tmp_path, monkeypatch):
    settings.DOCLING_ARTIFACTS_PATH = tmp_path / "models"
    attempts = 0

    def failed_run(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise subprocess.CalledProcessError(1, ["docling-tools"])

    monkeypatch.setattr(docling_models.subprocess, "run", failed_run)

    with pytest.raises(docling_models.ModelDownloadFailed):
        docling_models.ensure_models()
    with pytest.raises(docling_models.ModelDownloadFailed):
        docling_models.ensure_models()

    assert attempts == 2
    assert not list(settings.DOCLING_ARTIFACTS_PATH.glob("*.ready"))
