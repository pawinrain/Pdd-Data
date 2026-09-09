from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pdd_data_mcp.contracts.models import Scope
from pdd_data_mcp.errors import ValidationFailure


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_object(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def scope_key(scope: Scope) -> str:
    value: dict[str, object] = {
        "kind": scope.kind.value,
        "business_date": scope.business_date.isoformat(),
        "timezone": scope.timezone,
        "object_type": scope.object_type,
        "filters": dict(sorted(scope.filters.items())),
        "currency": scope.currency,
        "attribution": scope.attribution,
        "version": scope.version,
    }
    if scope.kind.value == "CUSTOM":
        value["start"] = scope.start.isoformat() if scope.start else None
        value["end"] = scope.end.isoformat() if scope.end else None
    return f"scope_{hash_object(value)[:32]}"


def encode_cursor(kind: str, query_hash: str, offset: int) -> str:
    core = {"kind": kind, "query_hash": query_hash, "offset": offset}
    envelope = {**core, "checksum": hash_object(core)}
    return base64.urlsafe_b64encode(canonical_json(envelope)).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, kind: str, query_hash: str) -> int:
    if not cursor or len(cursor) > 1024:
        raise ValidationFailure("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailure("invalid cursor") from exc
    if not isinstance(value, dict):
        raise ValidationFailure("invalid cursor")
    core = {
        "kind": value.get("kind"),
        "query_hash": value.get("query_hash"),
        "offset": value.get("offset"),
    }
    if value.get("checksum") != hash_object(core):
        raise ValidationFailure("cursor checksum mismatch")
    if core["kind"] != kind or core["query_hash"] != query_hash:
        raise ValidationFailure("cursor does not match query")
    offset = core["offset"]
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValidationFailure("invalid cursor offset")
    return offset


def encode_snapshot_list_cursor(query_hash: str, *, captured_at: datetime, snapshot_id: str) -> str:
    core = {
        "kind": "snapshot-list-keyset-v1",
        "query_hash": query_hash,
        "captured_at": captured_at.isoformat(),
        "snapshot_id": snapshot_id,
    }
    envelope = {**core, "checksum": hash_object(core)}
    return base64.urlsafe_b64encode(canonical_json(envelope)).decode("ascii").rstrip("=")


def decode_snapshot_list_cursor(cursor: str, *, query_hash: str) -> tuple[datetime, str]:
    if not cursor or len(cursor) > 1024:
        raise ValidationFailure("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailure("invalid cursor") from exc
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "query_hash",
        "captured_at",
        "snapshot_id",
        "checksum",
    }:
        raise ValidationFailure("invalid cursor")
    core = {
        "kind": value.get("kind"),
        "query_hash": value.get("query_hash"),
        "captured_at": value.get("captured_at"),
        "snapshot_id": value.get("snapshot_id"),
    }
    if value.get("checksum") != hash_object(core):
        raise ValidationFailure("cursor checksum mismatch")
    if core["kind"] != "snapshot-list-keyset-v1" or core["query_hash"] != query_hash:
        raise ValidationFailure("cursor does not match query")
    captured_raw = core["captured_at"]
    snapshot_id = core["snapshot_id"]
    if not isinstance(captured_raw, str) or not isinstance(snapshot_id, str):
        raise ValidationFailure("invalid cursor keyset")
    try:
        captured_at = datetime.fromisoformat(captured_raw)
    except ValueError as exc:
        raise ValidationFailure("invalid cursor keyset") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValidationFailure("invalid cursor keyset")
    if (
        not snapshot_id.startswith("s_")
        or len(snapshot_id) != 34
        or any(character not in "0123456789abcdef" for character in snapshot_id[2:])
    ):
        raise ValidationFailure("invalid cursor keyset")
    return captured_at, snapshot_id
