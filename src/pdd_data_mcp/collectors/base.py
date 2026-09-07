from __future__ import annotations

from typing import Protocol

from pdd_data_mcp.contracts.models import DatasetType, Scope, SnapshotDraft


class DatasetCollector(Protocol):
    async def collect(
        self,
        *,
        store_id: str,
        dataset_type: DatasetType,
        scope: Scope,
        limit: int,
        batch_id: str | None = None,
    ) -> SnapshotDraft: ...
