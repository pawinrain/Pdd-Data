from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pdd_data_mcp.contracts.models import (
    DatasetType,
    LatestSnapshotResult,
    ListSnapshotsResult,
    ReadSnapshotResult,
    Scope,
    SnapshotDraft,
    SnapshotManifest,
    ValidationReport,
)
from pdd_data_mcp.storage.records import RequestReservation


class SnapshotRepository(Protocol):
    def begin_request(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        idempotency_key: str,
        parameters: dict[str, object],
        scope_key: str,
    ) -> RequestReservation: ...

    def commit_reserved(
        self,
        reservation: RequestReservation,
        draft: SnapshotDraft,
        validation: ValidationReport,
    ) -> tuple[SnapshotManifest, list[str]]: ...

    def fail_request(self, reservation: RequestReservation, error_code: str) -> None: ...

    def get_manifest(self, snapshot_id: str) -> SnapshotManifest: ...

    def list_snapshots(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        captured_from: datetime | None,
        captured_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> ListSnapshotsResult: ...

    def read_snapshot(
        self, *, snapshot_id: str, cursor: str | None, page_size: int
    ) -> ReadSnapshotResult: ...

    def latest_snapshot(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        require_complete: bool,
    ) -> LatestSnapshotResult: ...
