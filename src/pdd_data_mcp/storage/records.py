from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class RequestReservation:
    request_id: str
    snapshot_id: str
    store_id: str
    dataset_type: str
    idempotency_key_hash: str
    parameter_hash: str
    scope_key: str
    request_file: str
    state: Literal["NEW", "COMMITTED", "RUNNING", "FAILED", "INTERRUPTED"]
    existing: dict[str, Any] | None = None
