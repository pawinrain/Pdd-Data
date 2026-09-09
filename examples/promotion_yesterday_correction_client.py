from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from mcp import Client, StdioServerParameters

DATASET = "promotion_overview"
SCOPE_VERSION = "d4-account-v3"
LEGACY_SCOPE_VERSION = "d4-account-v2"
TIMEZONE = "Asia/Shanghai"
EXPECTED_TOOLS = {
    "pdd_collect_snapshot",
    "pdd_get_capabilities",
    "pdd_get_connection_status",
    "pdd_get_latest_snapshot",
    "pdd_list_snapshots",
    "pdd_read_snapshot",
}
STOP_STATUSES = {
    "AUTH_REQUIRED",
    "IDENTITY_UNVERIFIED",
    "IDENTITY_MISMATCH",
    "PLATFORM_ERROR",
    "TIME_SCOPE_UNVERIFIED",
    "CAPTURE_TIMEOUT",
}
_EFFECT_FIELDS = {
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
_IDEMPOTENCY_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SNAPSHOT_ID = re.compile(r"^s_[0-9a-f]{32}$")
_INVALIDATION_ID = re.compile(r"^i_[0-9a-f]{32}$")
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _bounded_wait_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("wait seconds must be an integer") from exc
    if not 60 <= parsed <= 600:
        raise argparse.ArgumentTypeError("wait seconds must be between 60 and 600")
    return parsed


def _active_wire_item(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("effective_status", "ACTIVE") == "ACTIVE"
        and "invalidation" not in value
    )


def _legacy_check_passes(
    snapshot_id: str | None, *, latest_not_found: object, read_invalidated: object
) -> bool:
    return snapshot_id is not None and latest_not_found is True and read_invalidated is True


def parameters(config: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pdd_data_mcp.cli", "serve", "--config", str(config)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def structured(result: Any) -> dict[str, Any]:
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError("MCP call failed without structured content")
    return result.structured_content


def account_scope(
    run_business_date: date, kind: Literal["TODAY", "YESTERDAY"], *, version: str = SCOPE_VERSION
) -> dict[str, Any]:
    business_date = (
        run_business_date - timedelta(days=1) if kind == "YESTERDAY" else run_business_date
    )
    return {
        "kind": kind,
        "business_date": business_date.isoformat(),
        "timezone": TIMEZONE,
        "object_type": "ACCOUNT_ALL",
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": version,
        "start": None,
        "end": None,
    }


def _collection_plan(
    *, connection_id: str, run_business_date: date, idempotency_prefix: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    yesterday_scope = account_scope(run_business_date, "YESTERDAY")
    today_scope = account_scope(run_business_date, "TODAY")
    yesterday_calls = [
        {
            "connection_id": connection_id,
            "dataset_type": DATASET,
            "scope": yesterday_scope,
            "idempotency_key": f"{idempotency_prefix}-yesterday-{index:02d}",
            "limit": 1,
        }
        for index in range(1, 4)
    ]
    today_call = {
        "connection_id": connection_id,
        "dataset_type": DATASET,
        "scope": today_scope,
        "idempotency_key": f"{idempotency_prefix}-today-01",
        "limit": 1,
    }
    return yesterday_calls, today_call


async def read_snapshot(client: Client, snapshot_id: str) -> dict[str, Any]:
    cursor: str | None = None
    manifest: dict[str, Any] | None = None
    effective_status: str | None = None
    invalidation: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    page_count = 0
    while True:
        page = structured(
            await client.call_tool(
                "pdd_read_snapshot",
                {"snapshot_id": snapshot_id, "cursor": cursor, "page_size": 2},
            )
        )
        if page.get("snapshot_id") != snapshot_id or not isinstance(page.get("manifest"), dict):
            raise RuntimeError("snapshot read returned inconsistent metadata")
        page_status = page.get("effective_status", "ACTIVE")
        page_has_invalidation = "invalidation" in page
        page_invalidation = page.get("invalidation")
        if page_status not in {
            "ACTIVE",
            "SEMANTICALLY_INVALIDATED",
            "INVALIDATION_STATE_UNKNOWN",
        }:
            raise RuntimeError("snapshot effective status is invalid")
        if page_status == "SEMANTICALLY_INVALIDATED":
            if not isinstance(page_invalidation, dict):
                raise RuntimeError("snapshot effective status is invalid")
        elif page_has_invalidation:
            raise RuntimeError("active or unknown snapshot exposed invalidation metadata")
        if manifest is None:
            manifest = page["manifest"]
            effective_status = page_status
            invalidation = page_invalidation
        elif (
            manifest != page["manifest"]
            or effective_status != page_status
            or invalidation != page_invalidation
        ):
            raise RuntimeError("snapshot metadata changed across pages")
        page_data = page.get("data")
        if page_data is not None:
            if not isinstance(page_data, dict) or data is not None or cursor is not None:
                raise RuntimeError("snapshot object payload is invalid")
            data = page_data
        page_records = page.get("records")
        if not isinstance(page_records, list) or not all(
            isinstance(record, dict) for record in page_records
        ):
            raise RuntimeError("snapshot record page is invalid")
        records.extend(page_records)
        page_count += 1
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or page_count >= 100:
            raise RuntimeError("snapshot cursor is invalid")
        cursor = next_cursor
    assert manifest is not None and effective_status is not None
    result: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "manifest": manifest,
        "effective_status": effective_status,
        "data": data,
        "records": records,
        "page_count": page_count,
    }
    if invalidation is not None:
        result["invalidation"] = invalidation
    return result


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _missing_pairs_are_strict(metrics: dict[str, Any]) -> bool:
    reasons = metrics.get("missing_reasons")
    return (
        isinstance(reasons, dict)
        and set(metrics) == _EFFECT_FIELDS | {"missing_reasons"}
        and set(reasons) == {field for field in _EFFECT_FIELDS if metrics.get(field) is None}
        and all(
            reason in {"SOURCE_FIELD_MISSING", "SOURCE_VALUE_NULL"} for reason in reasons.values()
        )
        and any(metrics.get(field) is not None for field in _EFFECT_FIELDS)
    )


def _account_payload_is_valid(
    read: dict[str, Any], *, expected_business_date: date, kind: str
) -> bool:
    data = read.get("data")
    manifest = read.get("manifest")
    if not isinstance(data, dict) or not isinstance(manifest, dict) or read.get("records") != []:
        return False
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or not _missing_pairs_are_strict(metrics):
        return False
    source_updated_at = data.get("source_updated_at")
    missing_reason = data.get("source_updated_at_missing_reason")
    return (
        set(data)
        == {
            "entity_granularity",
            "business_date",
            "metrics",
            "observed_at",
            "source_updated_at",
            "source_updated_at_missing_reason",
        }
        and data.get("entity_granularity") == "ACCOUNT"
        and data.get("business_date") == expected_business_date.isoformat()
        and data.get("observed_at") == manifest.get("captured_at")
        and (
            (
                kind == "YESTERDAY"
                and source_updated_at is None
                and missing_reason == "SOURCE_VALUE_NULL"
            )
            or (
                kind == "TODAY"
                and _parse_datetime(source_updated_at) is not None
                and missing_reason is None
            )
        )
    )


def _time_evidence_is_valid(
    evidence: object,
    *,
    kind: str,
    business_date: date,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    if not isinstance(evidence, dict):
        return False
    request_hour = evidence.get("request_end_day_hour")
    row_count = evidence.get("response_hourly_row_count")
    first_hour = evidence.get("response_first_hour")
    last_hour = evidence.get("response_last_hour")
    today_cutoff = evidence.get("page_today_cutoff_hhmm")
    yesterday_cutoff = evidence.get("page_yesterday_cutoff_hhmm")
    if (
        isinstance(request_hour, bool)
        or not isinstance(request_hour, int)
        or not 0 <= request_hour <= 23
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or isinstance(first_hour, bool)
        or not isinstance(first_hour, int)
        or isinstance(last_hour, bool)
        or not isinstance(last_hour, int)
        or not isinstance(today_cutoff, str)
        or _HHMM.fullmatch(today_cutoff) is None
    ):
        return False
    if not isinstance(yesterday_cutoff, str) or _HHMM.fullmatch(yesterday_cutoff) is None:
        return False
    if (
        evidence.get("window_kind") != kind
        or _parse_date(evidence.get("request_start_date")) != business_date
        or _parse_date(evidence.get("request_end_date")) != business_date
        or _parse_date(evidence.get("response_business_date")) != business_date
        or evidence.get("page_semantics") != "SAME_PERIOD_COMPARISON"
        or evidence.get("business_timezone") != TIMEZONE
        or evidence.get("request_source") != "NETWORK_RESPONSE"
        or evidence.get("response_source") != "NETWORK_RESPONSE"
        or evidence.get("page_source") != "DOM"
        or first_hour != 0
        or last_hour != request_hour
        or row_count != last_hour + 1
    ):
        return False
    if kind == "YESTERDAY":
        expected_end = window_start + timedelta(hours=last_hour + 1)
        return (
            yesterday_cutoff == f"{last_hour:02d}:59"
            and window_end == expected_end
            and window_end.minute == window_end.second == window_end.microsecond == 0
        )
    return kind == "TODAY" and request_hour >= int(today_cutoff[:2])


def account_read_is_valid(
    read: dict[str, Any], *, requested_scope: dict[str, Any], captured: object
) -> bool:
    manifest = read.get("manifest")
    if not isinstance(manifest, dict):
        return False
    kind = requested_scope.get("kind")
    business_date = _parse_date(requested_scope.get("business_date"))
    metric_window = manifest.get("metric_window")
    if kind not in {"TODAY", "YESTERDAY"} or business_date is None:
        return False
    if not isinstance(metric_window, dict):
        return False
    start = _parse_datetime(metric_window.get("start"))
    end = _parse_datetime(metric_window.get("end"))
    captured_at = _parse_datetime(manifest.get("captured_at"))
    if start is None or end is None or captured_at is None:
        return False
    zone = ZoneInfo(TIMEZONE)
    start_local = start.astimezone(zone)
    end_local = end.astimezone(zone)
    capture_local = captured_at.astimezone(zone)
    field_sources = manifest.get("field_sources")
    expected_method = "MIXED" if kind == "YESTERDAY" else "NETWORK_RESPONSE"
    expected_end_source = "DOM" if kind == "YESTERDAY" else "NETWORK_RESPONSE"
    source_updated_at = _parse_datetime(manifest.get("source_updated_at"))
    common = (
        _active_wire_item(read)
        and manifest.get("dataset_type") == DATASET
        and manifest.get("source") == "PDD_BROWSER_CDP"
        and manifest.get("parser_version") == SCOPE_VERSION
        and manifest.get("capture_method") == expected_method
        and manifest.get("scope") == requested_scope
        and manifest.get("quality", {}).get("status") == "VALID"
        and manifest.get("quality", {}).get("identity") == "MATCHED"
        and manifest.get("quality", {}).get("coverage") == "COMPLETE"
        and captured == 1
        and manifest.get("coverage", {}).get("captured") == 1
        and manifest.get("coverage", {}).get("total_observed") == 1
        and manifest.get("coverage", {}).get("coverage") == "COMPLETE"
        and manifest.get("coverage", {}).get("truncated") is False
        and metric_window.get("kind") == kind
        and metric_window.get("timezone") == TIMEZONE
        and metric_window.get("window_complete") is False
        and metric_window.get("source_finalized") is False
        and start < end <= captured_at
        and start_local.date() == business_date
        and start_local.time().replace(tzinfo=None) == time.min
        and isinstance(field_sources, dict)
        and field_sources.get("metric_window.end") == expected_end_source
        and all(
            source == "NETWORK_RESPONSE"
            for field, source in field_sources.items()
            if field != "metric_window.end"
        )
        and _time_evidence_is_valid(
            manifest.get("promotion_account_time_evidence"),
            kind=kind,
            business_date=business_date,
            window_start=start,
            window_end=end,
        )
        and _account_payload_is_valid(
            read,
            expected_business_date=business_date,
            kind=kind,
        )
    )
    if not common:
        return False
    if kind == "YESTERDAY":
        return manifest.get(
            "source_updated_at"
        ) is None and capture_local.date() == business_date + timedelta(days=1)
    return (
        source_updated_at is not None
        and end == source_updated_at
        and end_local.date() == business_date
        and capture_local.date() == business_date
    )


def legacy_v2_read_is_invalidated(
    read: dict[str, Any], *, snapshot_id: str, store_id: str, run_business_date: date
) -> bool:
    manifest = read.get("manifest")
    invalidation = read.get("invalidation")
    expected_scope = account_scope(run_business_date, "YESTERDAY", version=LEGACY_SCOPE_VERSION)
    return (
        read.get("snapshot_id") == snapshot_id
        and read.get("effective_status") == "SEMANTICALLY_INVALIDATED"
        and isinstance(manifest, dict)
        and manifest.get("snapshot_id") == snapshot_id
        and manifest.get("store_id") == store_id
        and manifest.get("dataset_type") == DATASET
        and manifest.get("scope") == expected_scope
        and manifest.get("parser_version") == LEGACY_SCOPE_VERSION
        and isinstance(invalidation, dict)
        and isinstance(invalidation.get("invalidation_id"), str)
        and _INVALIDATION_ID.fullmatch(invalidation["invalidation_id"]) is not None
        and invalidation.get("snapshot_id") == snapshot_id
        and invalidation.get("store_id") == store_id
        and invalidation.get("dataset_type") == DATASET
        and invalidation.get("reason_code") == "METRIC_WINDOW_END_MISLABELED"
        and invalidation.get("prior_scope_version") == LEGACY_SCOPE_VERSION
        and invalidation.get("prior_parser_version") == LEGACY_SCOPE_VERSION
        and invalidation.get("replacement_scope_version") == SCOPE_VERSION
        and _parse_datetime(invalidation.get("invalidated_at")) is not None
    )


def _capture_public(result: dict[str, Any], *, kind: str, round_number: int) -> dict[str, Any]:
    return {
        "kind": kind,
        "round": round_number,
        "status": result.get("status"),
        "committed": result.get("committed"),
        "snapshot_id": result.get("snapshot_id"),
        "captured": result.get("captured"),
        "total_observed": result.get("total_observed"),
        "coverage": result.get("coverage"),
        "truncated": result.get("truncated"),
        "idempotent_replay": result.get("idempotent_replay"),
        "error_code": result.get("error_code"),
    }


def _capture_succeeded(result: dict[str, Any], *, replay: bool) -> bool:
    return (
        result.get("status") == "SUCCEEDED"
        and result.get("committed") is True
        and isinstance(result.get("snapshot_id"), str)
        and result.get("captured") == 1
        and result.get("total_observed") == 1
        and result.get("coverage") == "COMPLETE"
        and result.get("truncated") is False
        and result.get("idempotent_replay") is replay
        and result.get("error_code") is None
    )


async def _list_pagination_check(
    client: Client, *, store_id: str, expected_snapshot_ids: set[str]
) -> dict[str, object]:
    cursor: str | None = None
    seen: list[str] = []
    expected_active: set[str] = set()
    pages = 0
    cursor_observed = False
    while True:
        result = structured(
            await client.call_tool(
                "pdd_list_snapshots",
                {
                    "store_id": store_id,
                    "dataset_type": DATASET,
                    "cursor": cursor,
                    "limit": 2,
                },
            )
        )
        items = result.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise RuntimeError("snapshot list page is invalid")
        for item in items:
            snapshot_id = item.get("snapshot_id")
            if isinstance(snapshot_id, str):
                seen.append(snapshot_id)
                if snapshot_id in expected_snapshot_ids and _active_wire_item(item):
                    expected_active.add(snapshot_id)
        pages += 1
        next_cursor = result.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or pages >= 500:
            raise RuntimeError("snapshot list cursor is invalid")
        cursor_observed = True
        cursor = next_cursor
    passed = (
        cursor_observed
        and pages >= 2
        and len(seen) == len(set(seen))
        and expected_snapshot_ids <= set(seen)
        and expected_active == expected_snapshot_ids
    )
    return {
        "passed": passed,
        "pages_read": pages,
        "cursor_observed": cursor_observed,
        "listed_count": len(seen),
        "expected_active_count": len(expected_active),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    zone = ZoneInfo(TIMEZONE)
    run_business_date = datetime.now(zone).date()
    prefix = args.idempotency_prefix or f"d4yc-{uuid.uuid4().hex}"
    yesterday_scope = account_scope(run_business_date, "YESTERDAY")
    today_scope = account_scope(run_business_date, "TODAY")
    yesterday_calls, today_call = _collection_plan(
        connection_id=args.connection_id,
        run_business_date=run_business_date,
        idempotency_prefix=prefix,
    )
    captures: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    reads: dict[str, dict[str, Any]] = {}
    read_checks: list[bool] = []
    latest_checks: list[bool] = []
    replay_check = False
    tools_seen: list[str] = []
    tool_discovery_check = False
    capability_check = False
    connection_check = False
    legacy_check: dict[str, object] = {
        "requested": args.legacy_v2_snapshot_id is not None,
        "latest_not_found": None,
        "read_semantically_invalidated": None,
        "passed": False,
    }

    async with Client(parameters(args.config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        tools_seen = sorted(tool.name for tool in tools.tools)
        tool_discovery_check = len(tools_seen) == 6 and set(tools_seen) == EXPECTED_TOOLS
        capabilities = structured(await client.call_tool("pdd_get_capabilities", {}))
        detail = capabilities.get("dataset_details", {}).get(DATASET, {})
        capability_check = (
            isinstance(detail, dict)
            and detail.get("status") == "REAL_PROMOTION_WINDOWS"
            and detail.get("supported_window_kinds") == ["TODAY", "YESTERDAY"]
            and detail.get("entity_granularity") == "ACCOUNT"
            and detail.get("verified") is True
        )
        connection = structured(
            await client.call_tool(
                "pdd_get_connection_status", {"connection_id": args.connection_id}
            )
        )
        connection_check = (
            connection.get("status") == "NOT_CHECKED"
            and connection.get("store_id") == args.store_id
            and connection.get("real_collection_enabled") is True
            and connection.get("synthetic_test_enabled") is False
        )

        for index, call in enumerate(yesterday_calls, start=1):
            if datetime.now(zone).date() != run_business_date:
                raise RuntimeError("acceptance run crossed the Asia/Shanghai date boundary")
            result = structured(await client.call_tool("pdd_collect_snapshot", call))
            captures.append(_capture_public(result, kind="YESTERDAY", round_number=index))
            calls.append(call)
            if (
                not _capture_succeeded(result, replay=False)
                or result.get("status") in STOP_STATUSES
            ):
                break
            snapshot_id = str(result["snapshot_id"])
            read = await read_snapshot(client, snapshot_id)
            reads[snapshot_id] = read
            read_checks.append(
                account_read_is_valid(
                    read, requested_scope=yesterday_scope, captured=result.get("captured")
                )
            )
            latest = structured(
                await client.call_tool(
                    "pdd_get_latest_snapshot",
                    {
                        "store_id": args.store_id,
                        "dataset_type": DATASET,
                        "scope": yesterday_scope,
                        "require_complete": True,
                    },
                )
            )
            latest_snapshot = latest.get("snapshot")
            latest_checks.append(
                latest.get("status") == "FOUND"
                and _active_wire_item(latest_snapshot)
                and isinstance(latest_snapshot, dict)
                and latest_snapshot.get("snapshot_id") == snapshot_id
            )
            if index < 3:
                await asyncio.sleep(args.wait_seconds)

        if len(captures) == 3 and all(item.get("committed") is True for item in captures):
            replay = structured(await client.call_tool("pdd_collect_snapshot", yesterday_calls[0]))
            replay_check = _capture_succeeded(replay, replay=True) and replay.get(
                "snapshot_id"
            ) == captures[0].get("snapshot_id")
            await asyncio.sleep(args.wait_seconds)
            if datetime.now(zone).date() != run_business_date:
                raise RuntimeError("acceptance run crossed the Asia/Shanghai date boundary")
            today_result = structured(await client.call_tool("pdd_collect_snapshot", today_call))
            captures.append(_capture_public(today_result, kind="TODAY", round_number=1))
            calls.append(today_call)
            if _capture_succeeded(today_result, replay=False):
                today_id = str(today_result["snapshot_id"])
                today_read = await read_snapshot(client, today_id)
                reads[today_id] = today_read
                read_checks.append(
                    account_read_is_valid(
                        today_read,
                        requested_scope=today_scope,
                        captured=today_result.get("captured"),
                    )
                )
                latest = structured(
                    await client.call_tool(
                        "pdd_get_latest_snapshot",
                        {
                            "store_id": args.store_id,
                            "dataset_type": DATASET,
                            "scope": today_scope,
                            "require_complete": True,
                        },
                    )
                )
                latest_snapshot = latest.get("snapshot")
                latest_checks.append(
                    latest.get("status") == "FOUND"
                    and _active_wire_item(latest_snapshot)
                    and isinstance(latest_snapshot, dict)
                    and latest_snapshot.get("snapshot_id") == today_id
                )

        expected_ids = {
            str(item["snapshot_id"])
            for item in captures
            if isinstance(item.get("snapshot_id"), str)
        }
        list_check = await _list_pagination_check(
            client, store_id=args.store_id, expected_snapshot_ids=expected_ids
        )
        legacy_latest = structured(
            await client.call_tool(
                "pdd_get_latest_snapshot",
                {
                    "store_id": args.store_id,
                    "dataset_type": DATASET,
                    "scope": account_scope(
                        run_business_date,
                        "YESTERDAY",
                        version=LEGACY_SCOPE_VERSION,
                    ),
                    "require_complete": True,
                },
            )
        )
        latest_v2_not_found = (
            legacy_latest.get("status") == "NOT_FOUND" and legacy_latest.get("snapshot") is None
        )
        legacy_check["latest_not_found"] = latest_v2_not_found
        legacy_check["passed"] = _legacy_check_passes(
            args.legacy_v2_snapshot_id,
            latest_not_found=latest_v2_not_found,
            read_invalidated=None,
        )
        if args.legacy_v2_snapshot_id is not None:
            legacy_read = await read_snapshot(client, args.legacy_v2_snapshot_id)
            legacy_check = {
                "requested": True,
                "latest_not_found": latest_v2_not_found,
                "read_semantically_invalidated": legacy_v2_read_is_invalidated(
                    legacy_read,
                    snapshot_id=args.legacy_v2_snapshot_id,
                    store_id=args.store_id,
                    run_business_date=run_business_date,
                ),
                "passed": False,
            }
            legacy_check["passed"] = _legacy_check_passes(
                args.legacy_v2_snapshot_id,
                latest_not_found=legacy_check["latest_not_found"],
                read_invalidated=legacy_check["read_semantically_invalidated"],
            )
            reads[args.legacy_v2_snapshot_id] = legacy_read

    restart_checks: list[bool] = []
    async with Client(parameters(args.config), raise_exceptions=True) as restarted:
        for snapshot_id, original in reads.items():
            restarted_read = await read_snapshot(restarted, snapshot_id)
            restart_checks.append(restarted_read == original)

    date_stability_check = datetime.now(zone).date() == run_business_date
    unique_captures = [item for item in captures if item["kind"] == "YESTERDAY"]
    today_captures = [item for item in captures if item["kind"] == "TODAY"]
    new_snapshot_ids = {
        item["snapshot_id"] for item in captures if isinstance(item.get("snapshot_id"), str)
    }
    passed = (
        tool_discovery_check
        and capability_check
        and connection_check
        and len(unique_captures) == 3
        and all(_capture_succeeded(item, replay=False) for item in unique_captures)
        and len({item["snapshot_id"] for item in unique_captures}) == 3
        and replay_check
        and len(today_captures) == 1
        and _capture_succeeded(today_captures[0], replay=False)
        and len(new_snapshot_ids) == 4
        and len(read_checks) == 4
        and all(read_checks)
        and len(latest_checks) == 4
        and all(latest_checks)
        and list_check["passed"] is True
        and len(restart_checks) == len(reads)
        and all(restart_checks)
        and legacy_check["passed"] is True
        and date_stability_check
        and all(call.get("dataset_type") == DATASET for call in calls)
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "run_business_date": run_business_date.isoformat(),
        "date_stability_check": date_stability_check,
        "tools": tools_seen,
        "tool_discovery_check": tool_discovery_check,
        "capability_check": capability_check,
        "connection_check": connection_check,
        "capture_call_counts": {
            "yesterday_independent": len(unique_captures),
            "yesterday_replay": 1 if replay_check else 0,
            "today_regression": len(today_captures),
        },
        "captures": captures,
        "replay_first_key_check": replay_check,
        "read_contract_checks": read_checks,
        "latest_checks": latest_checks,
        "list_pagination_check": list_check,
        "restart_readback_checks": restart_checks,
        "legacy_v2_check": legacy_check,
        "collected_datasets": sorted({str(call["dataset_type"]) for call in calls}),
        "product_or_configuration_collection_calls": 0,
        "real_model_calls": 0,
        "platform_business_write_actions": 0,
        "execution_jobs": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MCP acceptance for corrected d4-account-v3 YESTERDAY semantics."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--store-id", required=True)
    parser.add_argument("--idempotency-prefix", default="")
    parser.add_argument("--legacy-v2-snapshot-id", required=True)
    parser.add_argument("--wait-seconds", type=_bounded_wait_seconds, default=60)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required for a real run")
    if args.idempotency_prefix and _IDEMPOTENCY_PREFIX.fullmatch(args.idempotency_prefix) is None:
        parser.error("--idempotency-prefix is not safe")
    if args.legacy_v2_snapshot_id and _SNAPSHOT_ID.fullmatch(args.legacy_v2_snapshot_id) is None:
        parser.error("--legacy-v2-snapshot-id is invalid")
    try:
        result = asyncio.run(run(args))
    except Exception:
        result = {
            "status": "FAIL",
            "error_code": "ACCEPTANCE_CLIENT_FAILURE",
            "real_model_calls": 0,
            "platform_business_write_actions": 0,
            "execution_jobs": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
