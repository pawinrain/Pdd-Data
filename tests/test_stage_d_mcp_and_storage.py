from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from conftest import make_repository, write_test_config

from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.application.core_sync import core_sync_status
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.validation import SnapshotValidator


def test_core_sync_summary_keeps_partial_success_explicit() -> None:
    assert (
        core_sync_status(
            {
                "store_overview": {"committed": True},
                "product_catalog": {"committed": True},
                "inventory": {"committed": False},
            }
        )
        == "PARTIAL_SUCCESS"
    )


def test_cross_store_snapshots_and_latest_are_isolated(tmp_path: Path) -> None:
    config = write_test_config(
        tmp_path / "config.toml", stores=("st_old_store", "st_current_store")
    )
    repository = make_repository(config)
    service = PddDataService(
        config=config,
        repository=repository,
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
    )
    point = Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 7))
    with repository.service_lock():
        repository.initialize()
        old = asyncio.run(
            service.collect_snapshot(
                connection_id="conn_01",
                dataset_type=DatasetType.PRODUCT_CATALOG,
                scope=point,
                limit=3,
                idempotency_key="same-business-key",
                batch_id="batch_old_store",
            )
        )
        current = asyncio.run(
            service.collect_snapshot(
                connection_id="conn_02",
                dataset_type=DatasetType.PRODUCT_CATALOG,
                scope=point,
                limit=3,
                idempotency_key="same-business-key",
                batch_id="batch_current_store",
            )
        )
        assert old.snapshot_id != current.snapshot_id
        assert old.batch_id == "batch_old_store"
        assert current.batch_id == "batch_current_store"
        old_list = service.list_snapshots(
            store_id="st_old_store",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        current_list = service.list_snapshots(
            store_id="st_current_store",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        assert [item.snapshot_id for item in old_list.items] == [old.snapshot_id]
        assert [item.snapshot_id for item in current_list.items] == [current.snapshot_id]
        read = service.read_snapshot(
            snapshot_id=current.snapshot_id,
            cursor=None,
            page_size=20,  # type: ignore[arg-type]
        )
        assert read.manifest["store_id"] == "st_current_store"
