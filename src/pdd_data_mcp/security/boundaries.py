from __future__ import annotations

from pathlib import Path
from typing import Any

from pdd_data_mcp.errors import ValidationFailure

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "secret",
        "session",
        "storage",
        "token",
    }
)


def safe_child(root: Path, *parts: str) -> Path:
    if any(not part or part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        raise ValidationFailure("unsafe path component")
    resolved_root = root.resolve(strict=False)
    candidate = resolved_root.joinpath(*parts).resolve(strict=False)
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise ValidationFailure("path escapes configured root")
    return candidate


def assert_no_sensitive_fields(value: Any, location: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in SENSITIVE_FIELD_NAMES or any(
                normalized.endswith(f"_{name}") for name in SENSITIVE_FIELD_NAMES
            ):
                raise ValidationFailure(f"sensitive field is forbidden at {location}.{key}")
            assert_no_sensitive_fields(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_sensitive_fields(item, f"{location}[{index}]")
