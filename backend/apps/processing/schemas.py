"""API schemas shared by the system settings and the document reprocess endpoints."""

from __future__ import annotations

from typing import Literal

from ninja import Field, Schema


class EnhanceSettingsOut(Schema):
    enabled: bool
    rotate: bool
    rotate_min_confidence: float
    deskew: bool
    deskew_min_angle: float
    deskew_max_angle: float
    crop: bool
    crop_max_fraction: float
    cleanup: bool
    cleanup_background: bool
    cleanup_contrast: bool
    cleanup_despeckle: bool
    cleanup_strength: Literal["low", "medium", "high"]
    remove_blank: bool
    blank_threshold: float


class EnhanceSettingsIn(Schema):
    """Partial update: omitted fields keep their current value."""

    enabled: bool | None = None
    rotate: bool | None = None
    rotate_min_confidence: float | None = Field(None, ge=0, le=30)
    deskew: bool | None = None
    deskew_min_angle: float | None = Field(None, ge=0, le=5)
    deskew_max_angle: float | None = Field(None, ge=0.5, le=30)
    crop: bool | None = None
    crop_max_fraction: float | None = Field(None, ge=0.05, le=0.6)
    cleanup: bool | None = None
    cleanup_background: bool | None = None
    cleanup_contrast: bool | None = None
    cleanup_despeckle: bool | None = None
    cleanup_strength: Literal["low", "medium", "high"] | None = None
    remove_blank: bool | None = None
    blank_threshold: float | None = Field(None, ge=0, le=2)

    def changes(self) -> dict[str, object]:
        return {k: v for k, v in self.dict().items() if v is not None}
