from __future__ import annotations

import asyncio
import json
import shutil
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
    SnapshotNotFoundError,
    StorageError,
    StorageFullError,
    ValidationFailure,
)
from pdd_data_mcp.storage import LocalFileSnapshotRepository
from pdd_data_mcp.utils import canonical_json, scope_key, sha256_bytes
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


def rewrite_committed_manifest(snapshot_dir: Path, value: dict[str, object]) -> None:
    manifest_path = snapshot_dir / "manifest.json"
    commit_path = snapshot_dir / "COMMIT.json"
    manifest_path.write_bytes(canonical_json(value) + b"\n")
    commit_value = json.loads(commit_path.read_text(encoding="utf-8"))
    commit_value["manifest_sha256"] = sha256_bytes(manifest_path.read_bytes())
    commit_path.write_bytes(canonical_json(commit_value) + b"\n")


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


def test_snapshot_list_keyset_survives_new_commit_between_pages(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        original_ids: set[str] = set()
        for index in range(3):
            manifest, _, _, _ = asyncio.run(
                commit_dataset(
                    repository,
                    DatasetType.STORE_OVERVIEW,
                    idempotency_key=f"keyset-original-{index}",
                )
            )
            original_ids.add(manifest.snapshot_id)
        first_page = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=2,
        )
        assert first_page.next_cursor is not None
        inserted, _, _, _ = asyncio.run(
            commit_dataset(
                repository,
                DatasetType.STORE_OVERVIEW,
                idempotency_key="keyset-inserted",
            )
        )
        second_page = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=first_page.next_cursor,
            limit=2,
        )
        first_ids = {item.snapshot_id for item in first_page.items}
        second_ids = {item.snapshot_id for item in second_page.items}

        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == original_ids
        assert inserted.snapshot_id not in second_ids
        assert second_page.next_cursor is None
        with pytest.raises(ValidationFailure, match="cursor does not match query"):
            repository.list_snapshots(
                store_id="st_test_001",
                dataset_type=DatasetType.INVENTORY,
                captured_from=None,
                captured_to=None,
                cursor=first_page.next_cursor,
                limit=2,
            )


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


@pytest.mark.parametrize("operation", ["list", "latest"])
def test_valid_json_catalog_forgery_is_rebuilt_before_query(
    config: AppConfig, operation: str
) -> None:
    repository = make_repository(config)
    scope = point_scope()
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="catalog-real")
        )
        catalog_path = config.storage.data_root / "_indexes" / "catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["items"][0]["captured_at"] = "2099-01-01T00:00:00+00:00"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        if operation == "list":
            result = repository.list_snapshots(
                store_id="st_test_001",
                dataset_type=DatasetType.STORE_OVERVIEW,
                captured_from=None,
                captured_to=None,
                cursor=None,
                limit=20,
            )
            assert [item.snapshot_id for item in result.items] == [manifest.snapshot_id]
            assert result.items[0].captured_at == manifest.captured_at
        else:
            result = repository.latest_snapshot(
                store_id="st_test_001",
                dataset_type=DatasetType.STORE_OVERVIEW,
                scope=scope,
                require_complete=False,
            )
            assert result.snapshot is not None
            assert result.snapshot.snapshot_id == manifest.snapshot_id
            assert result.snapshot.captured_at == manifest.captured_at


@pytest.mark.parametrize("tamper", ["empty", "drop-latest"])
def test_catalog_omission_is_rebuilt_from_complete_metadata_index(
    config: AppConfig, tamper: str
) -> None:
    repository = make_repository(config)
    scope = point_scope()
    with repository.service_lock():
        repository.initialize()
        first, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="omit-first")
        )
        second, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="omit-second")
        )
        catalog_path = config.storage.data_root / "_indexes" / "catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        if tamper == "empty":
            catalog["items"] = []
        else:
            del catalog["items"][0]
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        listed = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        latest = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope,
            require_complete=False,
        )

        assert {item.snapshot_id for item in listed.items} == {
            first.snapshot_id,
            second.snapshot_id,
        }
        assert latest.snapshot is not None
        assert latest.snapshot.snapshot_id == second.snapshot_id


def test_catalog_forgery_after_rebuild_fails_closed(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="catalog-bad")
        )
        catalog_path = config.storage.data_root / "_indexes" / "catalog.json"

        def tamper_catalog() -> None:
            value = json.loads(catalog_path.read_text(encoding="utf-8"))
            value["items"][0]["record_count"] += 1
            catalog_path.write_text(json.dumps(value), encoding="utf-8")

        tamper_catalog()
        original_rebuild = repository.rebuild_index

        def rebuild_then_tamper() -> dict[str, object]:
            result = original_rebuild()
            tamper_catalog()
            return result

        monkeypatch.setattr(repository, "rebuild_index", rebuild_then_tamper)
        with pytest.raises(StorageError, match="INDEX_REBUILD_FAILED"):
            repository.list_snapshots(
                store_id="st_test_001",
                dataset_type=DatasetType.STORE_OVERVIEW,
                captured_from=None,
                captured_to=None,
                cursor=None,
                limit=20,
            )


def test_valid_json_snapshot_alias_is_rebuilt_before_read(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        first, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="map-first")
        )
        second, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="map-second")
        )
        map_path = config.storage.data_root / "_indexes" / "snapshot-map.json"
        snapshot_map = json.loads(map_path.read_text(encoding="utf-8"))
        snapshot_map["snapshots"][first.snapshot_id] = snapshot_map["snapshots"][second.snapshot_id]
        map_path.write_text(json.dumps(snapshot_map), encoding="utf-8")

        read = repository.read_snapshot(
            snapshot_id=first.snapshot_id,
            cursor=None,
            page_size=20,
        )

        assert read.snapshot_id == first.snapshot_id
        assert read.manifest["snapshot_id"] == first.snapshot_id


def test_clean_queries_do_not_scan_all_snapshot_payloads(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(config)
    scope = point_scope()
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="no-full-scan")
        )

        def unexpected_full_scan() -> object:
            raise AssertionError("clean query attempted a full snapshot-tree scan")

        monkeypatch.setattr(repository, "_scan_snapshot_index", unexpected_full_scan)
        payload_verifications = 0
        original_verify_snapshot_dir = repository._verify_snapshot_dir

        def count_payload_verification(directory: Path):
            nonlocal payload_verifications
            payload_verifications += 1
            return original_verify_snapshot_dir(directory)

        monkeypatch.setattr(
            repository,
            "_verify_snapshot_dir",
            count_payload_verification,
        )
        listed = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        latest = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope,
            require_complete=False,
        )
        assert payload_verifications == 0
        read = repository.read_snapshot(
            snapshot_id=manifest.snapshot_id,
            cursor=None,
            page_size=20,
        )

        assert listed.items[0].snapshot_id == manifest.snapshot_id
        assert latest.snapshot is not None
        assert latest.snapshot.snapshot_id == manifest.snapshot_id
        assert read.snapshot_id == manifest.snapshot_id
        assert payload_verifications == 1


def test_snapshot_alias_after_rebuild_fails_closed(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        first, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="map-bad-first")
        )
        second, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="map-bad-second")
        )
        map_path = config.storage.data_root / "_indexes" / "snapshot-map.json"

        def tamper_map() -> None:
            value = json.loads(map_path.read_text(encoding="utf-8"))
            value["snapshots"][first.snapshot_id] = value["snapshots"][second.snapshot_id]
            map_path.write_text(json.dumps(value), encoding="utf-8")

        tamper_map()
        original_rebuild = repository.rebuild_index

        def rebuild_then_tamper() -> dict[str, object]:
            result = original_rebuild()
            tamper_map()
            return result

        monkeypatch.setattr(repository, "rebuild_index", rebuild_then_tamper)
        with pytest.raises(StorageError, match="INDEX_REBUILD_FAILED"):
            repository.read_snapshot(
                snapshot_id=first.snapshot_id,
                cursor=None,
                page_size=20,
            )


def test_random_missing_snapshot_id_does_not_rebuild_index(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="known-id")
        )
        rebuild_calls = 0

        def unexpected_rebuild() -> dict[str, object]:
            nonlocal rebuild_calls
            rebuild_calls += 1
            raise AssertionError("missing lookup must not rebuild a healthy index")

        monkeypatch.setattr(repository, "rebuild_index", unexpected_rebuild)
        with pytest.raises(SnapshotNotFoundError):
            repository.read_snapshot(
                snapshot_id=f"s_{'f' * 32}",
                cursor=None,
                page_size=20,
            )
        assert rebuild_calls == 0


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


@pytest.mark.parametrize("descriptor_tamper", ["delete", "duplicate"])
def test_manifest_requires_exact_unique_payload_and_validation_descriptors(
    config: AppConfig, descriptor_tamper: str
) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="descriptors")
        )
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        manifest_value = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        if descriptor_tamper == "delete":
            manifest_value["files"] = [
                item for item in manifest_value["files"] if item["name"] != "validation.json"
            ]
        else:
            manifest_value["files"].append(dict(manifest_value["files"][0]))
        rewrite_committed_manifest(snapshot_dir, manifest_value)

        with pytest.raises(SnapshotCorruptError, match="descriptors"):
            repository._verify_snapshot_dir(snapshot_dir)


def test_missing_validation_file_is_rejected(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(
                repository,
                DatasetType.STORE_OVERVIEW,
                idempotency_key="validation-missing",
            )
        )
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        (snapshot_dir / "validation.json").unlink()

        with pytest.raises(SnapshotCorruptError, match="missing snapshot file validation.json"):
            repository.read_snapshot(
                snapshot_id=manifest.snapshot_id,
                cursor=None,
                page_size=20,
            )


def test_validation_report_must_be_valid_and_match_manifest(config: AppConfig) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="validation-bad")
        )
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        validation_path = snapshot_dir / "validation.json"
        validation_value = json.loads(validation_path.read_text(encoding="utf-8"))
        validation_value["errors"] = ["forged-error"]
        validation_path.write_bytes(canonical_json(validation_value) + b"\n")
        manifest_value = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        validation_descriptor = next(
            item for item in manifest_value["files"] if item["name"] == "validation.json"
        )
        validation_descriptor["bytes"] = len(validation_path.read_bytes())
        validation_descriptor["sha256"] = sha256_bytes(validation_path.read_bytes())
        rewrite_committed_manifest(snapshot_dir, manifest_value)

        with pytest.raises(SnapshotCorruptError, match="validation report mismatch"):
            repository._verify_snapshot_dir(snapshot_dir)


def test_snapshot_moved_to_wrong_partition_is_never_indexed_or_read(config: AppConfig) -> None:
    repository = make_repository(config)
    scope = point_scope()
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="wrong-day")
        )
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        wrong_day = "31" if snapshot_dir.parent.name != "31" else "30"
        wrong_parent = snapshot_dir.parent.parent / wrong_day
        wrong_parent.mkdir(parents=True, exist_ok=True)
        moved = wrong_parent / snapshot_dir.name
        snapshot_dir.rename(moved)

        with pytest.raises(SnapshotCorruptError, match="physical partition mismatch"):
            repository._verify_snapshot_metadata(moved)
        rebuilt = repository.rebuild_index()
        assert moved.name in rebuilt["invalid"]
        listed = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        latest = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope,
            require_complete=False,
        )
        assert listed.items == []
        assert latest.status == "NOT_FOUND"
        with pytest.raises(SnapshotNotFoundError):
            repository.read_snapshot(
                snapshot_id=manifest.snapshot_id,
                cursor=None,
                page_size=20,
            )


def test_duplicate_committed_snapshot_id_invalidates_every_copy(config: AppConfig) -> None:
    repository = make_repository(config)
    scope = point_scope()
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="duplicate-id")
        )
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        duplicate_dir = snapshot_dir.parent / f"000000_000_copy_{manifest.snapshot_id}"
        shutil.copytree(snapshot_dir, duplicate_dir)

        with pytest.raises(SnapshotNotFoundError):
            repository.read_snapshot(
                snapshot_id=manifest.snapshot_id,
                cursor=None,
                page_size=20,
            )
        rebuilt = repository.rebuild_index()
        assert {snapshot_dir.name, duplicate_dir.name} <= set(rebuilt["invalid"])
        listed = repository.list_snapshots(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            captured_from=None,
            captured_to=None,
            cursor=None,
            limit=20,
        )
        latest = repository.latest_snapshot(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            scope=scope,
            require_complete=False,
        )
        assert listed.items == []
        assert latest.status == "NOT_FOUND"
        recovery = repository.recover()
        assert recovery["indexed_snapshots"] == 0
        assert len(recovery["quarantined"]) == 2
        assert not snapshot_dir.exists()
        assert not duplicate_dir.exists()


def test_commit_rejects_draft_scope_key_that_differs_from_reservation(
    config: AppConfig,
) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        scope = point_scope()
        draft = asyncio.run(
            SyntheticCollector().collect(
                store_id="st_test_001",
                dataset_type=DatasetType.STORE_OVERVIEW,
                scope=scope,
                limit=1,
            )
        )
        report = SnapshotValidator().validate(draft, synthetic_allowed=True)
        assert report.valid is True
        other_scope = scope.model_copy(update={"version": "2"})
        reservation = repository.begin_request(
            store_id="st_test_001",
            dataset_type=DatasetType.STORE_OVERVIEW,
            idempotency_key="scope-reservation-mismatch",
            parameters={"scope": scope.model_dump(mode="json")},
            scope_key=scope_key(other_scope),
        )

        with pytest.raises(ValidationFailure, match="draft scope does not match"):
            repository.commit_reserved(reservation, draft, report)

        assert list(config.storage.data_root.rglob("COMMIT.json")) == []


def test_forged_manifest_scope_key_with_matching_commit_checksum_is_rejected(
    config: AppConfig,
) -> None:
    repository = make_repository(config)
    with repository.service_lock():
        repository.initialize()
        manifest, _, _, _ = asyncio.run(
            commit_dataset(repository, DatasetType.STORE_OVERVIEW, idempotency_key="scope-forged")
        )
        snapshot_dir = next(config.storage.data_root.rglob(f"*{manifest.snapshot_id}"))
        manifest_path = snapshot_dir / "manifest.json"
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_value["scope_key"] = scope_key(manifest.scope.model_copy(update={"version": "2"}))
        rewrite_committed_manifest(snapshot_dir, manifest_value)

        with pytest.raises(SnapshotCorruptError, match="scope key mismatch"):
            repository._verify_snapshot_dir(snapshot_dir)
        rebuilt = repository.rebuild_index()
        assert snapshot_dir.name in rebuilt["invalid"]
        with pytest.raises(SnapshotNotFoundError):
            repository.read_snapshot(
                snapshot_id=manifest.snapshot_id,
                cursor=None,
                page_size=20,
            )


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
