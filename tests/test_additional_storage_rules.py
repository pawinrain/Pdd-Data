from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import make_repository, write_test_config
from test_storage import commit_dataset

from pdd_data_mcp.collectors import SyntheticCollector
from pdd_data_mcp.contracts.models import DatasetType, Scope, WindowKind
from pdd_data_mcp.errors import AuthorizationError, ResponseTooLargeError
from pdd_data_mcp.utils import scope_key
from pdd_data_mcp.validation import SnapshotValidator


def test_duplicate_business_ids_are_rejected() -> None:
    scope = Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 6))
    draft = asyncio.run(
        SyntheticCollector().collect(
            store_id="st_test_001",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            scope=scope,
            limit=2,
        )
    )
    assert isinstance(draft.payload, list)
    draft.payload[1]["product_id"] = draft.payload[0]["product_id"]
    draft.payload[1]["sku_id"] = draft.payload[0]["sku_id"]
    report = SnapshotValidator().validate(draft, synthetic_allowed=True)
    assert report.valid is False
    assert "duplicate product record" in report.errors


def test_today_metric_window_and_capture_time_are_separate() -> None:
    scope = Scope(kind=WindowKind.TODAY, business_date=date(2026, 9, 6))
    draft = asyncio.run(
        SyntheticCollector().collect(
            store_id="st_test_001",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope=scope,
            limit=50,
        )
    )
    assert draft.metric_window is not None
    assert draft.metric_window.end == draft.captured_at
    assert draft.metric_window.start != draft.captured_at
    assert draft.metric_window.window_complete is False
    assert draft.requested_at <= draft.capture_finished_at


def test_same_millisecond_produces_two_unique_snapshot_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_test_config(tmp_path / "config.toml")
    repository = make_repository(config)
    fixed = datetime(2026, 9, 6, 4, 0, 0, 123000, tzinfo=UTC)
    monkeypatch.setattr("pdd_data_mcp.collectors.synthetic.utc_now", lambda: fixed)
    with repository.service_lock():
        repository.initialize()
        first, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.INVENTORY, idempotency_key="same-ms-1")
        )
        second, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.INVENTORY, idempotency_key="same-ms-2")
        )
    directories = list(config.storage.data_root.rglob("*123_s_*"))
    assert first.snapshot_id != second.snapshot_id
    assert len(directories) == 2


def test_scope_isolation_and_complete_requirement(tmp_path: Path) -> None:
    config = write_test_config(tmp_path / "config.toml")
    repository = make_repository(config)
    captured_scope = Scope(
        kind=WindowKind.POINT_IN_TIME,
        business_date=date(2026, 9, 6),
        filters={"status": "ON_SALE"},
    )
    other_scope = Scope(
        kind=WindowKind.POINT_IN_TIME,
        business_date=date(2026, 9, 6),
        filters={"status": "OFF_SALE"},
    )
    with repository.service_lock():
        repository.initialize()
        reservation = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            idempotency_key="scope-partial",
            parameters={"scope": captured_scope.model_dump(mode="json")},
            scope_key=scope_key(captured_scope),
        )
        draft = asyncio.run(
            SyntheticCollector().collect(
                store_id="st_test_001",
                dataset_type=DatasetType.PRODUCT_CATALOG,
                scope=captured_scope,
                limit=50,
            )
        )
        report = SnapshotValidator().validate(draft, synthetic_allowed=True)
        repository.commit_reserved(reservation, draft, report)
        complete = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            scope=captured_scope,
            require_complete=True,
        )
        isolated = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.PRODUCT_CATALOG,
            scope=other_scope,
            require_complete=False,
        )
    assert complete.status == "PARTIAL_ONLY"
    assert isolated.status == "NOT_FOUND"


def test_snapshot_id_is_reauthorized_on_every_read(tmp_path: Path) -> None:
    config = write_test_config(tmp_path / "config.toml")
    writer = make_repository(config)
    with writer.service_lock():
        writer.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(writer, DatasetType.INVENTORY, idempotency_key="authorization")
        )
    unauthorized = make_repository(config, allowed=frozenset({"st_another"}))
    with unauthorized.service_lock():
        unauthorized.initialize()
        with pytest.raises(AuthorizationError):
            unauthorized.read_snapshot(snapshot_id=manifest.snapshot_id, cursor=None, page_size=20)


def test_oversize_object_response_is_rejected(tmp_path: Path) -> None:
    config = write_test_config(tmp_path / "config.toml", max_response_bytes=4096)
    repository = make_repository(config)
    scope = Scope(kind=WindowKind.POINT_IN_TIME, business_date=date(2026, 9, 6))
    with repository.service_lock():
        repository.initialize()
        reservation = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            idempotency_key="oversize",
            parameters={"oversize": True},
            scope_key=scope_key(scope),
        )
        draft = asyncio.run(
            SyntheticCollector().collect(
                store_id="st_test_001",
                dataset_type=DatasetType.STORE_OVERVIEW,
                scope=scope,
                limit=50,
            )
        )
        assert isinstance(draft.payload, dict)
        metric = draft.payload["metrics"]["visitor_count"]
        draft.payload["metrics"] = {f"metric_{index:03d}": metric for index in range(100)}
        report = SnapshotValidator().validate(draft, synthetic_allowed=True)
        assert report.valid
        manifest, _ = repository.commit_reserved(reservation, draft, report)
        with pytest.raises(ResponseTooLargeError):
            repository.read_snapshot(snapshot_id=manifest.snapshot_id, cursor=None, page_size=20)
