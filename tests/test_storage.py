from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import make_repository

from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.config import AppConfig
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    SnapshotCorruptError,
    StorageFullError,
)
from pdd_data_mcp.storage import LocalFileSnapshotRepository
from pdd_data_mcp.utils import scope_key
from pdd_data_mcp.validation import SnapshotValidator


def point_scope() -> Scope:
    return Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 6))


async def commit_dataset(
    repository: LocalFileSnapshotRepository,
    dataset: DatasetType,
    *,
    idempotency_key: str,
    limit: int = 50,
):
    scope = point_scope()
    parameters: dict[str, object] = {
        "store_id": "st_test_001",
        "dataset_type": dataset.value,
        "scope": scope.model_dump(mode="json"),
        "limit": limit,
    }
    reservation = repository.begin_request(
        store_id="st_test_001",
        dataset_type=dataset,
        idempotency_key=idempotency_key,
        parameters=parameters,
        scope_key=scope_key(scope),
    )
    draft = await SyntheticCollector().collect(
        store_id="st_test_001", dataset_type=dataset, scope=scope, limit=limit
    )
    report = SnapshotValidator().validate(draft, synthetic_allowed=True)
    manifest, warnings = repository.commit_reserved(reservation, draft, report)
    return manifest, warnings, reservation, parameters


def test_jsonl_commit_partition_pagination_and_checksums(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, warnings, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.PRODUCT_CATALOG, idempotency_key="page-1")
        )
        assert warnings == []
        assert manifest.coverage.total_observed == 55
        assert manifest.coverage.captured == 50
        assert manifest.coverage.truncated is True
        first = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id, cursor=None, page_size=20
        )
        second = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id, cursor=first.next_cursor, page_size=20
        )
        third = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id, cursor=second.next_cursor, page_size=20
        )
        assert [len(first.records), len(second.records), len(third.records)] == [20, 20, 10]
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        assert "2026" not in str(snapshot_dir) or snapshot_dir.is_dir()
        assert (snapshot_dir / "records.jsonl").is_file()
        assert (snapshot_dir / "manifest.json").is_file()
        assert (snapshot_dir / "validation.json").is_file()
        assert (snapshot_dir / "COMMIT.json").is_file()


def test_json_object_snapshot_keeps_missing_values_null(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="object-1")
        )
        result = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id, cursor=None, page_size=20
        )
        assert result.data is not None
        assert result.data["metrics"]["order_count"] is None
        assert result.manifest["missing_fields"] == ["order_count"]
        assert result.manifest["metric_window"] is None


def test_idempotency_reservation_replays_and_conflicts(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, parameters = asyncio.run(
            commit_dataset(repository, DatasetType.INVENTORY, idempotency_key="stable-key")
        )
        replay = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.INVENTORY,
            idempotency_key="stable-key",
            parameters=parameters,
            scope_key=scope_key(point_scope()),
        )
        assert replay.state == "COMMITTED"
        assert replay.snapshot_id == manifest.snapshot_id
        parameters["limit"] = 49
        with pytest.raises(IdempotencyConflictError):
            repository.begin_request(
                store_id="st_test_001",
                dataset_type=DatasetType.INVENTORY,
                idempotency_key="stable-key",
                parameters=parameters,
                scope_key=scope_key(point_scope()),
            )


def test_multiple_snapshots_never_overwrite_or_sum_cumulative_metrics(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        first, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.PROMOTION_OVERVIEW, idempotency_key="metric-1")
        )
        second, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.PROMOTION_OVERVIEW, idempotency_key="metric-2")
        )
        assert first.snapshot_id != second.snapshot_id
        listed = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        assert len(listed.items) == 2
        values = []
        for item in listed.items:
            read = repository.read_snapshot(snapshot_id=item.snapshot_id, cursor=None, page_size=20)
            values.append(read.data["metrics"]["ad_spend"]["value"])
        assert values == [12345, 12345]


def test_latest_survives_failed_attempt_and_index_rebuild(config: AppConfig) -> None:
    repository = make_repository(config)
    scope = point_scope()
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="good")
        )
        failed = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            idempotency_key="failed",
            parameters={"different": True},
            scope_key=scope_key(scope),
        )
        repository.fail_request(failed, "INJECTED_FAILURE")
        (config.storage.data_root / "_indexes" / "catalog.json").write_text(
            "broken", encoding="utf-8"
        )
        latest = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope,
            require_complete=False,
        )
        assert latest.snapshot is not None
        assert latest.snapshot.snapshot_id == manifest.snapshot_id


def test_corruption_and_missing_commit_are_never_read(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="corrupt")
        )
        directory = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        (directory / "data.json").write_text('{"metrics":{}}\n', encoding="utf-8")
        with pytest.raises(SnapshotCorruptError):
            repository.read_snapshot(snapshot_id=manifest.snapshot_id, cursor=None, page_size=20)
        (directory / "COMMIT.json").unlink()
        repository.rebuild_index()
        listed = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        assert listed.items == []


def test_store_authorization_is_enforced(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        with pytest.raises(AuthorizationError):
            repository.list_snapshots(
                store_id="st_other",
                dataset_type=DatasetType.INVENTORY,
                captured_from=None,
                captured_to=None,
                cursor=None,
                limit=20,
            )


def test_quota_failure_does_not_claim_a_commit(tmp_path: Path) -> None:
    from conftest import write_test_config

    config = write_test_config(tmp_path / "config.toml", max_bytes=1)
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        with pytest.raises(StorageFullError):
            asyncio.run(
                commit_dataset(repository, DatasetType.PRODUCT_CATALOG, idempotency_key="full")
            )
        assert list(config.storage.data_root.rglob("COMMIT.json")) == []


def test_partition_uses_shanghai_date_not_utc_date(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        scope = point_scope()
        reservation = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.INVENTORY,
            idempotency_key="midnight",
            parameters={"midnight": True},
            scope_key=scope_key(scope),
        )
        draft = asyncio.run(
            SyntheticCollector().collect(
                store_id="st_test_001",
                dataset_type=DatasetType.INVENTORY,
                scope=scope,
                limit=1,
            )
        )
        moment = datetime(2026, 9, 6, 16, 30, tzinfo=UTC)
        draft.captured_at = moment
        draft.capture_started_at = moment
        draft.capture_finished_at = moment
        draft.requested_at = moment
        draft.payload[0]["observed_at"] = moment.isoformat()
        report = SnapshotValidator().validate(draft, synthetic_allowed=True)
        manifest, _ = repository.commit_reserved(reservation, draft, report)
        directory = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        parts = directory.parts
        assert (parts[-4], parts[-3], parts[-2]) == ("2026", "09", "07")
