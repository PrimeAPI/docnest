"""Scan enhancement settings (kept free of image libraries so the web process can import them)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

Strength = Literal["low", "medium", "high"]
STRENGTHS: tuple[Strength, ...] = ("low", "medium", "high")


@dataclass(frozen=True)
class EnhanceSettings:
    enabled: bool = True
    rotate: bool = True
    rotate_min_confidence: float = 3.0  # Tesseract "Orientation confidence"
    deskew: bool = True
    deskew_min_angle: float = 0.2  # degrees; smaller skew is left alone
    deskew_max_angle: float = 8.0  # degrees; larger estimates are distrusted
    crop: bool = True
    crop_max_fraction: float = 0.35  # never cut more than this share of a page dimension
    cleanup: bool = True
    cleanup_background: bool = True
    cleanup_contrast: bool = True
    cleanup_despeckle: bool = True
    cleanup_strength: Strength = "medium"
    remove_blank: bool = True
    blank_threshold: float = 0.003  # percent of the page that must be content ink to keep it

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object, base: EnhanceSettings | None = None) -> EnhanceSettings:
        """Validated settings from (partial) JSON; unknown keys and bad values fall back to `base`."""
        values = (base or cls()).to_json()
        if isinstance(data, dict):
            for f in fields(cls):
                if f.name not in data:
                    continue
                value, default = data[f.name], values[f.name]
                if isinstance(default, bool):
                    if isinstance(value, bool):
                        values[f.name] = value
                elif isinstance(default, float):
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        values[f.name] = float(value)
                elif f.name == "cleanup_strength" and value in STRENGTHS:
                    values[f.name] = value
        values["rotate_min_confidence"] = _clamp(values["rotate_min_confidence"], 0.0, 30.0)
        values["deskew_min_angle"] = _clamp(values["deskew_min_angle"], 0.0, 5.0)
        values["deskew_max_angle"] = _clamp(values["deskew_max_angle"], 0.5, 30.0)
        values["crop_max_fraction"] = _clamp(values["crop_max_fraction"], 0.05, 0.6)
        values["blank_threshold"] = _clamp(values["blank_threshold"], 0.0, 2.0)
        return cls(**values)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
