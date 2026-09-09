from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdd_data_mcp.contracts.models import (
    Coverage,
    CoverageStatus,
    DatasetType,
    ReadSnapshotResult,
    Scope,
    SnapshotInvalidationRecord,
    SnapshotSummary,
    WindowKind,
)
from pdd_data_mcp.errors import ValidationFailure
from pdd_data_mcp.parsers import money_to_cents, ratio_to_string
from pdd_data_mcp.schema_export import SCHEMAS
from pdd_data_mcp.utils import decode_cursor, encode_cursor


def test_scope_requires_timezone_aware_custom_dates() -> None:
    with pytest.raises(ValidationError):
        Scope(
            kind=WindowKind.CUSTOM,
            business_date=date(2026, 9, 6),
            start=datetime(2026, 9, 6),
            end=datetime(2026, 9, 7),
        )
    scope = Scope(
        kind=WindowKind.CUSTOM,
        business_date=date(2026, 9, 6),
        start=datetime(2026, 9, 6, tzinfo=UTC),
        end=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert scope.end - scope.start == timedelta(days=1)


def test_coverage_enforces_truncation_for_55_of_50() -> None:
    with pytest.raises(ValidationError):
        Coverage(
            total_observed=55,
            captured=50,
            limit=50,
            pages_read=1,
            coverage=CoverageStatus.COMPLETE,
            truncated=False,
        )
    valid = Coverage(
        total_observed=55,
        captured=50,
        limit=50,
        pages_read=1,
        coverage=CoverageStatus.TRUNCATED,
        truncated=True,
    )
    assert valid.coverage is CoverageStatus.TRUNCATED


def test_unknown_total_does_not_become_zero() -> None:
    value = Coverage(
        total_observed=None,
        captured=3,
        limit=50,
        pages_read=1,
        coverage=CoverageStatus.UNKNOWN,
        truncated=False,
    )
    assert value.total_observed is None


def test_parsers_do_not_use_float_for_money_and_ratio() -> None:
    assert money_to_cents("1.23").value == 123
    approximate = money_to_cents("1.2万")
    assert (approximate.value, approximate.precision) == (1_200_000, "APPROXIMATE")
    assert ratio_to_string("12.50", percent=True).value == "0.125"


def test_cursor_is_query_bound_and_tamper_evident() -> None:
    cursor = encode_cursor("snapshot-list", "abc", 10)
    assert decode_cursor(cursor, kind="snapshot-list", query_hash="abc") == 10
    with pytest.raises(ValidationFailure):
        decode_cursor(cursor, kind="snapshot-records", query_hash="abc")
    with pytest.raises(ValidationFailure):
        decode_cursor(
            cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
            kind="snapshot-list",
            query_hash="abc",
        )


def invalidation() -> SnapshotInvalidationRecord:
    return SnapshotInvalidationRecord(
        invalidation_id=f"i_{'1' * 32}",
        snapshot_id=f"s_{'2' * 32}",
        store_id="st_contract",
        dataset_type=DatasetType.PROMOTION_OVERVIEW,
        invalidated_at=datetime(2026, 9, 8, tzinfo=UTC),
        reason_code="METRIC_WINDOW_END_MISLABELED",
        prior_scope_version="d4-account-v2",
        prior_parser_version="d4-account-v2",
        replacement_scope_version="d4-account-v3",
    )


@pytest.mark.parametrize(
    "invalidation_update",
    [
        {"snapshot_id": f"s_{'3' * 32}"},
        {"store_id": "st_other"},
        {"dataset_type": DatasetType.STORE_OVERVIEW},
    ],
)
def test_snapshot_summary_rejects_mismatched_invalidation_identity(
    invalidation_update: dict[str, object],
) -> None:
    record = invalidation().model_copy(update=invalidation_update)
    with pytest.raises(ValidationError, match="invalidation identity"):
        SnapshotSummary(
            snapshot_id=f"s_{'2' * 32}",
            store_id="st_contract",
            dataset_type=DatasetType.PROMOTION_OVERVIEW,
            scope_key=f"scope_{'4' * 32}",
            captured_at=datetime(2026, 9, 8, tzinfo=UTC),
            committed_at=datetime(2026, 9, 8, tzinfo=UTC),
            source="PDD_BROWSER_CDP",
            quality_status="VALID",
            effective_status="SEMANTICALLY_INVALIDATED",
            invalidation=record,
            coverage=CoverageStatus.COMPLETE,
            record_count=1,
        )


def test_read_result_rejects_outer_and_invalidation_identity_mismatches() -> None:
    record = invalidation()
    manifest = {
        "snapshot_id": record.snapshot_id,
        "store_id": record.store_id,
        "dataset_type": record.dataset_type.value,
    }
    with pytest.raises(ValidationError, match="snapshot identity"):
        ReadSnapshotResult(
            snapshot_id=f"s_{'3' * 32}",
            manifest=manifest,
        )
    with pytest.raises(ValidationError, match="invalidation identity"):
        ReadSnapshotResult(
            snapshot_id=record.snapshot_id,
            manifest=manifest,
            effective_status="SEMANTICALLY_INVALIDATED",
            invalidation=record.model_copy(update={"store_id": "st_other"}),
        )


def test_checked_in_schemas_match_runtime_models() -> None:
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    for name, model in SCHEMAS.items():
        expected = json.dumps(
            model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
        )
        assert (schema_root / name).read_text(encoding="utf-8") == expected + "\n"
