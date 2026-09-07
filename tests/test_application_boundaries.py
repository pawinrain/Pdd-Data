from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from conftest import make_repository, write_test_config

from pdd_data_mcp.application import PddDataService
from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.validation import SnapshotValidator


def make_service(tmp_path: Path, *, test_mode: bool) -> tuple[PddDataService, object]:
    config = write_test_config(tmp_path / "config.toml", test_mode=test_mode)
    repository = make_repository(config)
    service = PddDataService(
        config=config,
        repository=repository,
        synthetic_collector=SyntheticCollector(),
        validator=SnapshotValidator(),
    )
    return service, repository


def test_real_collection_is_disabled_without_synthetic_fallback(tmp_path: Path) -> None:
    service, repository = make_service(tmp_path, test_mode=False)
    scope = Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 6))
    with repository.service_lock():
        repository.initialize()
        result = asyncio.run(
            service.collect_snapshot(
                connection_id="conn_01",
                dataset_type=DatasetType.STORE_OVERVIEW,
                scope=scope,
                limit=50,
                idempotency_key="must-not-collect",
            )
        )
    assert result.status == "REAL_COLLECTION_DISABLED"
    assert result.committed is False
    assert list((service.config.storage.data_root / "snapshots").rglob("COMMIT.json")) == []


def test_reserved_dataset_reports_unverified_and_capabilities_separate_status(
    tmp_path: Path,
) -> None:
    service, repository = make_service(tmp_path, test_mode=True)
    scope = Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 6))
    with repository.service_lock():
        repository.initialize()
        result = asyncio.run(
            service.collect_snapshot(
                connection_id="conn_01",
                dataset_type=DatasetType.PRODUCT_METRICS,
                scope=scope,
                limit=50,
                idempotency_key="unverified-dataset",
            )
        )
    capabilities = service.capabilities()
    assert result.status == "DATASET_UNVERIFIED"
    assert result.committed is False
    assert capabilities.real_collection == "DISABLED"
    assert capabilities.datasets["product_metrics"] == "UNAVAILABLE"


def test_connection_status_never_claims_connected(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, test_mode=True)
    status = service.connection_status("conn_01")
    assert status.status == "NOT_CHECKED"
    assert status.real_collection_enabled is False
