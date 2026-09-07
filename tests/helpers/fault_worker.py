from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from pdd_data_mcp.config import load_config
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.storage import LocalFileSnapshotRepository
from pdd_data_mcp.utils import canonical_json, scope_key


def main() -> None:
    config = load_config(Path(sys.argv[1]))
    stage = sys.argv[2]
    repository = LocalFileSnapshotRepository(
        config.storage,
        allowed_store_ids=config.allowed_store_ids,
        max_response_bytes=config.collection.max_mcp_response_bytes,
    )
    with repository.service_lock():
        repository.initialize()
        scope = Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 6))
        reservation = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.INVENTORY,
            idempotency_key=f"fault-{stage}",
            parameters={"stage": stage},
            scope_key=scope_key(scope),
        )
        temporary = config.storage.data_root / "_tmp" / reservation.snapshot_id
        temporary.mkdir()
        body = canonical_json({"product_id": "partial"}) + b"\n"
        (temporary / "records.jsonl").write_bytes(body)
        if stage == "after-manifest":
            (temporary / "manifest.json").write_bytes(b"{}\n")
        os._exit(91)


if __name__ == "__main__":
    main()
