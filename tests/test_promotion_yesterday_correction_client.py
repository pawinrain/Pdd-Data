from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import date
from typing import Any, cast

import pytest

from examples.promotion_yesterday_correction_client import (
    DATASET,
    LEGACY_SCOPE_VERSION,
    SCOPE_VERSION,
    _active_wire_item,
    _bounded_wait_seconds,
    _capture_public,
    _capture_succeeded,
    _collection_plan,
    _legacy_check_passes,
    _list_pagination_check,
    account_read_is_valid,
    account_scope,
    legacy_v2_read_is_invalidated,
    read_snapshot,
)

RUN_DATE = date(2026, 9, 8)
SNAPSHOT_ID = "s_11111111111111111111111111111111"
SECOND_SNAPSHOT_ID = "s_22222222222222222222222222222222"


def metrics() -> dict[str, object]:
    fields = {
        "spend_cents",
        "order_spend_cents",
        "order_spend_roi",
        "order_spend_net_roi",
        "net_order_count",
        "order_count",
        "gmv_cents",
        "net_gmv_cents",
        "impression_count",
        "click_count",
        "settlement_roi",
        "settlement_order_count",
    }
    values: dict[str, object] = {field: None for field in fields}
    values["spend_cents"] = 123
    return {
        **values,
        "missing_reasons": {
            field: "SOURCE_VALUE_NULL" for field, value in values.items() if value is None
        },
    }


def account_read(*, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    assert kind in {"TODAY", "YESTERDAY"}
    scope = account_scope(RUN_DATE, cast(Any, kind))
    if kind == "YESTERDAY":
        business_date = "2026-09-07"
        start = "2026-09-07T00:00:00+08:00"
        end = "2026-09-07T17:00:00+08:00"
        captured_at = "2026-09-08T18:00:00+08:00"
        source_updated_at = None
        source_missing = "SOURCE_VALUE_NULL"
        capture_method = "MIXED"
        end_source = "DOM"
        today_cutoff = "16:42"
        yesterday_cutoff = "16:59"
    else:
        business_date = "2026-09-08"
        start = "2026-09-08T00:00:00+08:00"
        end = "2026-09-08T16:42:31+08:00"
        captured_at = "2026-09-08T16:45:00+08:00"
        source_updated_at = end
        source_missing = None
        capture_method = "NETWORK_RESPONSE"
        end_source = "NETWORK_RESPONSE"
        today_cutoff = "16:42"
        yesterday_cutoff = "16:59"
    evidence = {
        "window_kind": kind,
        "request_start_date": business_date,
        "request_end_date": business_date,
        "request_end_day_hour": 16,
        "response_business_date": business_date,
        "page_today_cutoff_hhmm": today_cutoff,
        "page_yesterday_cutoff_hhmm": yesterday_cutoff,
        "page_semantics": "SAME_PERIOD_COMPARISON",
        "business_timezone": "Asia/Shanghai",
        "request_source": "NETWORK_RESPONSE",
        "response_source": "NETWORK_RESPONSE",
        "page_source": "DOM",
        "response_hourly_row_count": 17,
        "response_first_hour": 0,
        "response_last_hour": 16,
    }
    read = {
        "snapshot_id": SNAPSHOT_ID,
        "manifest": {
            "snapshot_id": SNAPSHOT_ID,
            "store_id": "st_current",
            "dataset_type": DATASET,
            "source": "PDD_BROWSER_CDP",
            "parser_version": SCOPE_VERSION,
            "capture_method": capture_method,
            "scope": scope,
            "captured_at": captured_at,
            "source_updated_at": source_updated_at,
            "promotion_account_time_evidence": evidence,
            "metric_window": {
                "kind": kind,
                "timezone": "Asia/Shanghai",
                "start": start,
                "end": end,
                "window_complete": False,
                "source_finalized": False,
            },
            "field_sources": {
                "metric_window.end": end_source,
                "metrics.spend_cents": "NETWORK_RESPONSE",
            },
            "quality": {
                "status": "VALID",
                "identity": "MATCHED",
                "coverage": "COMPLETE",
            },
            "coverage": {
                "captured": 1,
                "total_observed": 1,
                "coverage": "COMPLETE",
                "truncated": False,
            },
        },
        "data": {
            "entity_granularity": "ACCOUNT",
            "business_date": business_date,
            "metrics": metrics(),
            "observed_at": captured_at,
            "source_updated_at": source_updated_at,
            "source_updated_at_missing_reason": source_missing,
        },
        "records": [],
        "page_count": 1,
    }
    return read, scope


class FakeResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.is_error = False
        self.structured_content = payload


class FakeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> FakeResult:
        self.calls.append((name, arguments))
        return FakeResult(self.responses.pop(0))


def test_account_scopes_are_exact_and_v3_by_default() -> None:
    yesterday = account_scope(RUN_DATE, "YESTERDAY")
    today = account_scope(RUN_DATE, "TODAY")

    assert yesterday["business_date"] == "2026-09-07"
    assert today["business_date"] == "2026-09-08"
    assert yesterday["version"] == today["version"] == SCOPE_VERSION
    assert yesterday["object_type"] == "ACCOUNT_ALL"
    assert yesterday["start"] is yesterday["end"] is None


def test_collection_plan_has_exact_three_yesterday_and_one_today() -> None:
    yesterday, today = _collection_plan(
        connection_id="conn_current",
        run_business_date=RUN_DATE,
        idempotency_prefix="acceptance",
    )

    assert len(yesterday) == 3
    assert len({call["idempotency_key"] for call in yesterday}) == 3
    assert all(call["scope"]["kind"] == "YESTERDAY" for call in yesterday)
    assert today["scope"]["kind"] == "TODAY"
    assert {call["dataset_type"] for call in [*yesterday, today]} == {DATASET}
    assert all(call["limit"] == 1 for call in [*yesterday, today])


@pytest.mark.parametrize("kind", ["YESTERDAY", "TODAY"])
def test_account_read_accepts_strict_v3_time_evidence(kind: str) -> None:
    read, scope = account_read(kind=kind)

    assert account_read_is_valid(read, requested_scope=scope, captured=1)


def test_active_wire_compatibility_defaults_old_keyset_and_rejects_invalidation_key() -> None:
    assert _active_wire_item({"snapshot_id": SNAPSHOT_ID})
    assert _active_wire_item({"snapshot_id": SNAPSHOT_ID, "effective_status": "ACTIVE"})
    assert not _active_wire_item({"snapshot_id": SNAPSHOT_ID, "invalidation": None})
    assert not _active_wire_item(
        {"snapshot_id": SNAPSHOT_ID, "effective_status": "SEMANTICALLY_INVALIDATED"}
    )


@pytest.mark.parametrize(("raw", "expected"), [("60", 60), ("600", 600)])
def test_wait_seconds_accepts_only_bounded_values(raw: str, expected: int) -> None:
    assert _bounded_wait_seconds(raw) == expected


@pytest.mark.parametrize("raw", ["59", "601", "not-an-integer"])
def test_wait_seconds_rejects_out_of_range_or_invalid_values(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _bounded_wait_seconds(raw)


def test_yesterday_end_is_half_open_hour_after_response_last_hour() -> None:
    read, scope = account_read(kind="YESTERDAY")
    window = read["manifest"]["metric_window"]
    window["end"] = "2026-09-07T16:59:00+08:00"

    assert not account_read_is_valid(read, requested_scope=scope, captured=1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("response_hourly_row_count", 16),
        ("response_first_hour", 1),
        ("response_last_hour", 15),
        ("page_yesterday_cutoff_hhmm", "16:58"),
        ("request_end_day_hour", 15),
    ],
)
def test_yesterday_rejects_inconsistent_hourly_evidence(field: str, value: object) -> None:
    read, scope = account_read(kind="YESTERDAY")
    read["manifest"]["promotion_account_time_evidence"][field] = value

    assert not account_read_is_valid(read, requested_scope=scope, captured=1)


@pytest.mark.parametrize(
    "kind,method,end_source",
    [
        ("YESTERDAY", "NETWORK_RESPONSE", "DOM"),
        ("YESTERDAY", "MIXED", "NETWORK_RESPONSE"),
        ("TODAY", "MIXED", "NETWORK_RESPONSE"),
        ("TODAY", "NETWORK_RESPONSE", "DOM"),
    ],
)
def test_account_read_rejects_capture_provenance_drift(
    kind: str, method: str, end_source: str
) -> None:
    read, scope = account_read(kind=kind)
    read["manifest"]["capture_method"] = method
    read["manifest"]["field_sources"]["metric_window.end"] = end_source

    assert not account_read_is_valid(read, requested_scope=scope, captured=1)


def test_legacy_v2_requires_append_only_semantic_invalidation() -> None:
    legacy_scope = account_scope(RUN_DATE, "YESTERDAY", version=LEGACY_SCOPE_VERSION)
    read = {
        "snapshot_id": SNAPSHOT_ID,
        "effective_status": "SEMANTICALLY_INVALIDATED",
        "manifest": {
            "snapshot_id": SNAPSHOT_ID,
            "store_id": "st_current",
            "dataset_type": DATASET,
            "scope": legacy_scope,
            "parser_version": LEGACY_SCOPE_VERSION,
        },
        "invalidation": {
            "invalidation_id": "i_11111111111111111111111111111111",
            "snapshot_id": SNAPSHOT_ID,
            "store_id": "st_current",
            "dataset_type": DATASET,
            "reason_code": "METRIC_WINDOW_END_MISLABELED",
            "prior_scope_version": LEGACY_SCOPE_VERSION,
            "prior_parser_version": LEGACY_SCOPE_VERSION,
            "replacement_scope_version": SCOPE_VERSION,
            "invalidated_at": "2026-09-08T12:00:00+08:00",
        },
    }

    assert legacy_v2_read_is_invalidated(
        read,
        snapshot_id=SNAPSHOT_ID,
        store_id="st_current",
        run_business_date=RUN_DATE,
    )
    wrong = deepcopy(read)
    wrong["effective_status"] = "ACTIVE"
    assert not legacy_v2_read_is_invalidated(
        wrong,
        snapshot_id=SNAPSHOT_ID,
        store_id="st_current",
        run_business_date=RUN_DATE,
    )


def test_legacy_check_cannot_pass_without_required_snapshot_read() -> None:
    assert not _legacy_check_passes(
        None,
        latest_not_found=True,
        read_invalidated=True,
    )
    assert not _legacy_check_passes(
        SNAPSHOT_ID,
        latest_not_found=True,
        read_invalidated=None,
    )
    assert _legacy_check_passes(
        SNAPSHOT_ID,
        latest_not_found=True,
        read_invalidated=True,
    )


@pytest.mark.parametrize("field", ["invalidation_id", "invalidated_at"])
def test_legacy_v2_requires_complete_append_only_record(field: str) -> None:
    read = {
        "snapshot_id": SNAPSHOT_ID,
        "effective_status": "SEMANTICALLY_INVALIDATED",
        "manifest": {
            "snapshot_id": SNAPSHOT_ID,
            "store_id": "st_current",
            "dataset_type": DATASET,
            "scope": account_scope(RUN_DATE, "YESTERDAY", version=LEGACY_SCOPE_VERSION),
            "parser_version": LEGACY_SCOPE_VERSION,
        },
        "invalidation": {
            "invalidation_id": "i_11111111111111111111111111111111",
            "snapshot_id": SNAPSHOT_ID,
            "store_id": "st_current",
            "dataset_type": DATASET,
            "reason_code": "METRIC_WINDOW_END_MISLABELED",
            "prior_scope_version": LEGACY_SCOPE_VERSION,
            "prior_parser_version": LEGACY_SCOPE_VERSION,
            "replacement_scope_version": SCOPE_VERSION,
            "invalidated_at": "2026-09-08T12:00:00+08:00",
        },
    }
    cast(dict[str, object], read["invalidation"]).pop(field)

    assert not legacy_v2_read_is_invalidated(
        read,
        snapshot_id=SNAPSHOT_ID,
        store_id="st_current",
        run_business_date=RUN_DATE,
    )


def test_read_snapshot_normalizes_omitted_active_status_across_pages() -> None:
    manifest = {"snapshot_id": SNAPSHOT_ID}
    client = FakeClient(
        [
            {
                "snapshot_id": SNAPSHOT_ID,
                "manifest": manifest,
                "data": None,
                "records": [{"row": 1}],
                "next_cursor": "cursor-1",
            },
            {
                "snapshot_id": SNAPSHOT_ID,
                "manifest": manifest,
                "data": None,
                "records": [{"row": 2}],
                "next_cursor": None,
            },
        ]
    )

    result = asyncio.run(read_snapshot(cast(Any, client), SNAPSHOT_ID))

    assert result["effective_status"] == "ACTIVE"
    assert "invalidation" not in result
    assert result["records"] == [{"row": 1}, {"row": 2}]
    assert result["page_count"] == 2
    assert client.calls[1][1]["cursor"] == "cursor-1"


def test_read_snapshot_rejects_invalidation_key_for_active_wire_result() -> None:
    client = FakeClient(
        [
            {
                "snapshot_id": SNAPSHOT_ID,
                "manifest": {"snapshot_id": SNAPSHOT_ID},
                "invalidation": None,
                "data": None,
                "records": [],
                "next_cursor": None,
            }
        ]
    )

    with pytest.raises(RuntimeError, match="exposed invalidation metadata"):
        asyncio.run(read_snapshot(cast(Any, client), SNAPSHOT_ID))


def test_list_pagination_requires_unique_rows_and_all_new_snapshots_active() -> None:
    client = FakeClient(
        [
            {
                "items": [
                    {"snapshot_id": SNAPSHOT_ID},
                ],
                "next_cursor": "cursor-1",
            },
            {
                "items": [
                    {"snapshot_id": SECOND_SNAPSHOT_ID},
                ],
                "next_cursor": None,
            },
        ]
    )

    result = asyncio.run(
        _list_pagination_check(
            cast(Any, client),
            store_id="st_current",
            expected_snapshot_ids={SNAPSHOT_ID, SECOND_SNAPSHOT_ID},
        )
    )

    assert result == {
        "passed": True,
        "pages_read": 2,
        "cursor_observed": True,
        "listed_count": 2,
        "expected_active_count": 2,
    }
    assert all(call[0] == "pdd_list_snapshots" for call in client.calls)


def test_capture_public_and_success_gate_distinguish_replay() -> None:
    raw = {
        "status": "SUCCEEDED",
        "committed": True,
        "snapshot_id": SNAPSHOT_ID,
        "captured": 1,
        "total_observed": 1,
        "coverage": "COMPLETE",
        "truncated": False,
        "idempotent_replay": False,
        "error_code": None,
    }
    public = _capture_public(raw, kind="YESTERDAY", round_number=1)
    assert _capture_succeeded(public, replay=False)
    assert not _capture_succeeded(public, replay=True)
    replay = {**raw, "idempotent_replay": True}
    assert _capture_succeeded(replay, replay=True)
