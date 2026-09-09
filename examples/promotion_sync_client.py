from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp import Client, StdioServerParameters

CURRENT_DATASETS = (
    "promotion_overview",
    "product_metrics",
    "promotion_configuration",
)
LIST_DATASETS = CURRENT_DATASETS
EXPECTED_TOOLS = {
    "pdd_collect_snapshot",
    "pdd_get_capabilities",
    "pdd_get_connection_status",
    "pdd_get_latest_snapshot",
    "pdd_list_snapshots",
    "pdd_read_snapshot",
}
EXPECTED_CAPABILITIES = {
    "promotion_overview": "REAL_PROMOTION_WINDOWS",
    "product_metrics": "REAL_PROMOTED_PRODUCT_METRICS",
    "campaign_metrics": "UNAVAILABLE",
    "promotion_configuration": "REAL_PROMOTION_CONFIGURATION_CURRENT",
}
STOP_STATUSES = {
    "AUTH_REQUIRED",
    "IDENTITY_UNVERIFIED",
    "IDENTITY_MISMATCH",
    "PLATFORM_ERROR",
}


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


async def read_snapshot(client: Client, snapshot_id: str) -> dict[str, Any]:
    cursor: str | None = None
    manifest: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    data: dict[str, Any] | None = None
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
        if manifest is None:
            manifest = page["manifest"]
        elif manifest != page["manifest"]:
            raise RuntimeError("snapshot manifest changed across pages")
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
    assert manifest is not None
    return {
        "snapshot_id": snapshot_id,
        "manifest": manifest,
        "data": data,
        "records": records,
        "page_count": page_count,
    }


def scope(dataset: str, *, run_business_date: date, yesterday: bool = False) -> dict[str, Any]:
    business_date = run_business_date - timedelta(days=1) if yesterday else run_business_date
    kind = (
        "POINT_IN_TIME"
        if dataset == "promotion_configuration"
        else "YESTERDAY"
        if yesterday
        else "TODAY"
    )
    object_type = "ACCOUNT_ALL" if dataset == "promotion_overview" else "PROMOTED_PRODUCT_ALL"
    return {
        "kind": kind,
        "business_date": business_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "object_type": object_type,
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": "d4-account-v3" if dataset == "promotion_overview" else "1",
        "start": None,
        "end": None,
    }


def _association_rows(read: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (str(row["ad_id"]), str(row["campaign_id"]), str(row["platform_product_id"]))
        for row in read.get("records", [])
        if isinstance(row, dict)
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


def _missing_pairs_are_strict(value: dict[str, Any], *, business_fields: set[str]) -> bool:
    reasons = value.get("missing_reasons")
    return (
        isinstance(reasons, dict)
        and set(reasons) == {field for field in business_fields if value.get(field) is None}
        and all(
            reason in {"SOURCE_FIELD_MISSING", "SOURCE_VALUE_NULL"} for reason in reasons.values()
        )
        and any(value.get(field) is not None for field in business_fields)
    )


def _record_payload_is_strict(read: dict[str, Any], *, dataset: str) -> bool:
    manifest = read.get("manifest")
    records = read.get("records")
    if not isinstance(manifest, dict) or not isinstance(records, list) or not records:
        return False
    if dataset == "product_metrics":
        expected_fields = {
            "entity_granularity",
            "ad_id",
            "campaign_id",
            "platform_product_id",
            "metrics",
            "observed_at",
            "source_updated_at",
        }
        for row in records:
            if not isinstance(row, dict) or set(row) != expected_fields:
                return False
            metrics = row.get("metrics")
            if (
                row.get("entity_granularity") != "PROMOTED_PRODUCT"
                or row.get("observed_at") != manifest.get("captured_at")
                or row.get("source_updated_at") != manifest.get("source_updated_at")
                or not isinstance(metrics, dict)
                or set(metrics) != _EFFECT_FIELDS | {"missing_reasons"}
                or not _missing_pairs_are_strict(metrics, business_fields=_EFFECT_FIELDS)
            ):
                return False
        return True
    if dataset == "promotion_configuration":
        configuration_fields = {
            "max_cost_cents",
            "target_roi",
            "agent_bid",
            "ad_status",
        }
        expected_fields = {
            "entity_granularity",
            "ad_id",
            "campaign_id",
            "platform_product_id",
            "configuration_observed_at",
            "missing_reasons",
        } | configuration_fields
        for row in records:
            if (
                not isinstance(row, dict)
                or set(row) != expected_fields
                or row.get("entity_granularity") != "PROMOTED_PRODUCT"
                or row.get("configuration_observed_at") != manifest.get("captured_at")
                or not _missing_pairs_are_strict(row, business_fields=configuration_fields)
            ):
                return False
        return True
    return False


def _account_payload_is_strict(
    read: dict[str, Any], *, expected_business_date: str, yesterday: bool
) -> bool:
    data = read.get("data")
    if not isinstance(data, dict) or read.get("records") != []:
        return False
    metrics = data.get("metrics")
    expected_metric_fields = _EFFECT_FIELDS | {"missing_reasons"}
    if not isinstance(metrics, dict) or set(metrics) != expected_metric_fields:
        return False
    missing_reasons = metrics.get("missing_reasons")
    if not isinstance(missing_reasons, dict):
        return False
    if not _missing_pairs_are_strict(metrics, business_fields=_EFFECT_FIELDS):
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
        and data.get("business_date") == expected_business_date
        and isinstance(data.get("observed_at"), str)
        and (
            (yesterday and source_updated_at is None and missing_reason == "SOURCE_VALUE_NULL")
            or (not yesterday and isinstance(source_updated_at, str) and missing_reason is None)
        )
    )


def _manifest_contract_is_valid(
    read: dict[str, Any],
    *,
    dataset: str,
    requested_scope: dict[str, Any],
    captured: object,
) -> bool:
    manifest = read.get("manifest")
    if not isinstance(manifest, dict):
        return False
    manifest_scope = manifest.get("scope")
    manifest_window = manifest.get("metric_window")
    expected_kind = requested_scope["kind"]
    expected_capture_method = (
        "MIXED"
        if dataset == "promotion_overview" and expected_kind == "YESTERDAY"
        else "NETWORK_RESPONSE"
    )
    common = (
        manifest.get("dataset_type") == dataset
        and manifest.get("source") == "PDD_BROWSER_CDP"
        and manifest.get("capture_method") == expected_capture_method
        and manifest.get("quality", {}).get("status") == "VALID"
        and manifest.get("quality", {}).get("identity") == "MATCHED"
        and manifest.get("quality", {}).get("coverage") == "COMPLETE"
        and isinstance(manifest_scope, dict)
        and manifest_scope == requested_scope
        and (
            (dataset == "promotion_overview" and captured == 1 and read.get("records") == [])
            or (
                dataset != "promotion_overview"
                and len(read.get("records", [])) == captured
                and read.get("data") is None
            )
        )
    )
    if not common:
        return False
    if dataset == "promotion_configuration":
        try:
            captured_local_date = (
                datetime.fromisoformat(str(manifest.get("captured_at")))
                .astimezone(ZoneInfo("Asia/Shanghai"))
                .date()
            )
        except ValueError:
            return False
        return (
            manifest_window is None
            and manifest.get("source_updated_at") is None
            and read.get("data") is None
            and captured_local_date.isoformat() == requested_scope["business_date"]
            and _record_payload_is_strict(read, dataset=dataset)
        )
    if not isinstance(manifest_window, dict):
        return False
    if dataset == "product_metrics":
        try:
            start = datetime.fromisoformat(manifest_window["start"])
            end = datetime.fromisoformat(manifest_window["end"])
            captured_at = datetime.fromisoformat(str(manifest.get("captured_at")))
            product_source_updated_at = datetime.fromisoformat(
                str(manifest.get("source_updated_at"))
            )
            expected_date = datetime.fromisoformat(requested_scope["business_date"]).date()
        except (KeyError, TypeError, ValueError):
            return False
        start_local = start.astimezone(ZoneInfo("Asia/Shanghai"))
        end_local = end.astimezone(ZoneInfo("Asia/Shanghai"))
        return (
            manifest_window.get("kind") == expected_kind
            and manifest_window.get("window_complete") is False
            and manifest_window.get("source_finalized") is False
            and read.get("data") is None
            and start_local.date() == expected_date
            and start_local.time().replace(tzinfo=None) == datetime.min.time()
            and end_local.date() == expected_date
            and end == captured_at
            and start < product_source_updated_at <= end
            and _record_payload_is_strict(read, dataset=dataset)
        )
    if dataset == "promotion_overview":
        date_value = requested_scope["business_date"]
        manifest_source_updated_at = manifest.get("source_updated_at")
        try:
            start = datetime.fromisoformat(manifest_window["start"])
            end = datetime.fromisoformat(manifest_window["end"])
            expected_date = datetime.fromisoformat(date_value).date()
            captured_at = datetime.fromisoformat(str(manifest.get("captured_at")))
            parsed_source_updated_at = (
                datetime.fromisoformat(manifest_source_updated_at)
                if isinstance(manifest_source_updated_at, str)
                else None
            )
        except (KeyError, TypeError, ValueError):
            return False
        account_data = read.get("data")
        field_sources = manifest.get("field_sources")
        expected_capture_date = (
            expected_date + timedelta(days=1) if expected_kind == "YESTERDAY" else expected_date
        )
        return (
            manifest_window.get("kind") == expected_kind
            and manifest_window.get("window_complete") is False
            and manifest_window.get("source_finalized") is False
            and start.tzinfo is not None
            and end.tzinfo is not None
            and start.astimezone(ZoneInfo("Asia/Shanghai")).date() == expected_date
            and start.astimezone(ZoneInfo("Asia/Shanghai")).time().replace(tzinfo=None)
            == datetime.min.time()
            and end.astimezone(ZoneInfo("Asia/Shanghai")).date() == expected_date
            and end > start
            and captured_at.astimezone(ZoneInfo("Asia/Shanghai")).date() == expected_capture_date
            and (
                (
                    expected_kind == "TODAY"
                    and isinstance(manifest_source_updated_at, str)
                    and end == parsed_source_updated_at
                )
                or (
                    expected_kind == "YESTERDAY"
                    and manifest_source_updated_at is None
                    and end.second == 0
                    and end.microsecond == 0
                )
            )
            and isinstance(account_data, dict)
            and account_data.get("observed_at") == manifest.get("captured_at")
            and isinstance(field_sources, dict)
            and (
                (expected_kind == "YESTERDAY" and field_sources.get("metric_window.end") == "DOM")
                or (
                    expected_kind == "TODAY"
                    and field_sources.get("metric_window.end") == "NETWORK_RESPONSE"
                )
            )
            and _account_payload_is_strict(
                read,
                expected_business_date=date_value,
                yesterday=expected_kind == "YESTERDAY",
            )
        )
    return False


async def run(args: argparse.Namespace) -> dict[str, Any]:
    shanghai = ZoneInfo("Asia/Shanghai")
    run_business_date = datetime.now(shanghai).date()
    prefix = args.idempotency_prefix or f"d4-{uuid.uuid4().hex}"
    calls: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    reads: dict[str, dict[str, Any]] = {}
    latest_checks: list[bool] = []
    read_contract_checks: list[bool] = []
    read_page_counts: list[int] = []
    list_checks: dict[str, bool] = {}
    tools_seen: list[str] = []
    capabilities: dict[str, Any] = {}
    connection_status: dict[str, Any] = {}
    negative_product_yesterday_check = False
    catalog_read: dict[str, Any] | None = None
    catalog_snapshot_id: str | None = None
    catalog_contract_check = False

    async with Client(parameters(args.config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        tools_seen = sorted(tool.name for tool in tools.tools)
        capabilities = structured(await client.call_tool("pdd_get_capabilities", {}))
        connection_status = structured(
            await client.call_tool(
                "pdd_get_connection_status", {"connection_id": args.connection_id}
            )
        )
        negative_product_yesterday = structured(
            await client.call_tool(
                "pdd_collect_snapshot",
                {
                    "connection_id": args.connection_id,
                    "dataset_type": "product_metrics",
                    "scope": scope(
                        "product_metrics",
                        run_business_date=run_business_date,
                        yesterday=True,
                    ),
                    "idempotency_key": f"{prefix}-product-yesterday-negative",
                    "limit": args.limit,
                },
            )
        )
        negative_product_yesterday_check = (
            negative_product_yesterday.get("status") == "DATASET_UNVERIFIED"
            and negative_product_yesterday.get("committed") is False
            and negative_product_yesterday.get("snapshot_id") is None
            and negative_product_yesterday.get("error_code") == "REAL_DATASET_OR_SCOPE_NOT_ADAPTED"
        )
        catalog_list = structured(
            await client.call_tool(
                "pdd_list_snapshots",
                {
                    "store_id": args.store_id,
                    "dataset_type": "product_catalog",
                    "limit": 1,
                },
            )
        )
        catalog_items = catalog_list.get("items")
        if isinstance(catalog_items, list) and len(catalog_items) == 1:
            candidate_id = catalog_items[0].get("snapshot_id")
            if isinstance(candidate_id, str):
                catalog_snapshot_id = candidate_id
                catalog_read = await read_snapshot(client, candidate_id)
                catalog_manifest = catalog_read["manifest"]
                catalog_scope = catalog_manifest.get("scope")
                catalog_contract_check = (
                    catalog_manifest.get("store_id") == args.store_id
                    and catalog_manifest.get("dataset_type") == "product_catalog"
                    and catalog_manifest.get("source") == "PDD_BROWSER_CDP"
                    and catalog_manifest.get("quality", {}).get("status") == "VALID"
                    and catalog_manifest.get("quality", {}).get("coverage") == "COMPLETE"
                    and isinstance(catalog_scope, dict)
                    and catalog_scope.get("kind") == "POINT_IN_TIME"
                    and catalog_scope.get("business_date") == run_business_date.isoformat()
                    and bool(catalog_read["records"])
                )
        sequence: list[tuple[int, str, bool]] = [
            (round_number, dataset, False)
            for round_number in range(1, args.captures_per_dataset + 1)
            for dataset in CURRENT_DATASETS
        ]
        # One extra historical account capture proves the only evidenced
        # historical window without pretending that product history is enabled.
        sequence.append((0, "promotion_overview", True))
        for index, (round_number, dataset, yesterday) in enumerate(sequence):
            if datetime.now(shanghai).date() != run_business_date:
                raise RuntimeError("acceptance run crossed the Asia/Shanghai date boundary")
            batch = f"batch_{prefix}_history" if yesterday else f"batch_{prefix}_{round_number:02d}"
            call = {
                "connection_id": args.connection_id,
                "dataset_type": dataset,
                "scope": scope(
                    dataset,
                    run_business_date=run_business_date,
                    yesterday=yesterday,
                ),
                "idempotency_key": (
                    f"{prefix}-{dataset}-yesterday"
                    if yesterday
                    else f"{prefix}-{dataset}-{round_number:02d}"
                ),
                "batch_id": batch,
                "limit": args.limit,
            }
            calls.append(call)
            result = structured(await client.call_tool("pdd_collect_snapshot", call))
            public = {
                "round": round_number,
                "window": call["scope"]["kind"],
                "dataset_type": dataset,
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
            captures.append(public)
            if not result.get("committed") or result.get("status") in STOP_STATUSES:
                break
            snapshot_id = result["snapshot_id"]
            read = await read_snapshot(client, snapshot_id)
            reads[snapshot_id] = read
            read_contract_checks.append(
                _manifest_contract_is_valid(
                    read,
                    dataset=dataset,
                    requested_scope=call["scope"],
                    captured=result.get("captured"),
                )
            )
            read_page_counts.append(read["page_count"])
            latest = structured(
                await client.call_tool(
                    "pdd_get_latest_snapshot",
                    {
                        "store_id": args.store_id,
                        "dataset_type": dataset,
                        "scope": call["scope"],
                        "require_complete": True,
                    },
                )
            )
            latest_snapshot = latest.get("snapshot")
            latest_checks.append(
                latest.get("status") == "FOUND"
                and isinstance(latest_snapshot, dict)
                and latest_snapshot.get("snapshot_id") == snapshot_id
            )
            if (
                index < len(sequence) - 1
                and args.wait_seconds
                and result.get("idempotent_replay") is not True
            ):
                await asyncio.sleep(args.wait_seconds)

        for dataset in LIST_DATASETS:
            expected_ids = {
                item["snapshot_id"]
                for item in captures
                if item["dataset_type"] == dataset and isinstance(item.get("snapshot_id"), str)
            }
            cursor: str | None = None
            seen_ids: list[str] = []
            pages_read = 0
            cursor_observed = False
            while True:
                listed = structured(
                    await client.call_tool(
                        "pdd_list_snapshots",
                        {
                            "store_id": args.store_id,
                            "dataset_type": dataset,
                            "cursor": cursor,
                            "limit": 1,
                        },
                    )
                )
                items = listed.get("items")
                if not isinstance(items, list):
                    raise RuntimeError("snapshot list page is invalid")
                seen_ids.extend(
                    str(item["snapshot_id"])
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("snapshot_id"), str)
                )
                pages_read += 1
                next_cursor = listed.get("next_cursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor or pages_read >= 100:
                    raise RuntimeError("snapshot list cursor is invalid")
                cursor_observed = True
                cursor = next_cursor
            list_checks[dataset] = (
                len(expected_ids) == (4 if dataset == "promotion_overview" else 3)
                and expected_ids <= set(seen_ids)
                and len(seen_ids) == len(set(seen_ids))
                and cursor_observed
                and pages_read >= 2
            )

    restart_readback: list[bool] = []
    idempotent_replay: list[bool] = []
    async with Client(parameters(args.config), raise_exceptions=True) as restarted:
        for call, capture in zip(calls, captures, strict=True):
            snapshot_id = capture.get("snapshot_id")
            if not capture.get("committed") or not isinstance(snapshot_id, str):
                continue
            read = await read_snapshot(restarted, snapshot_id)
            replay = structured(await restarted.call_tool("pdd_collect_snapshot", call))
            restart_readback.append(
                read == reads.get(snapshot_id)
                and _manifest_contract_is_valid(
                    read,
                    dataset=str(capture["dataset_type"]),
                    requested_scope=call["scope"],
                    captured=capture.get("captured"),
                )
            )
            idempotent_replay.append(
                replay.get("snapshot_id") == snapshot_id and replay.get("idempotent_replay") is True
            )
    date_stability_check = datetime.now(shanghai).date() == run_business_date

    association_checks: list[bool] = []
    catalog_association_checks: list[bool] = []
    catalog_product_ids = {
        str(row["platform_product_id"])
        for row in (catalog_read or {}).get("records", [])
        if isinstance(row, dict) and isinstance(row.get("platform_product_id"), str | int)
    }
    for round_number in range(1, args.captures_per_dataset + 1):
        round_captures = {
            item["dataset_type"]: item for item in captures if item["round"] == round_number
        }
        product_id = round_captures.get("product_metrics", {}).get("snapshot_id")
        config_id = round_captures.get("promotion_configuration", {}).get("snapshot_id")
        if isinstance(product_id, str) and isinstance(config_id, str):
            product_rows = _association_rows(reads[product_id])
            configuration_rows = _association_rows(reads[config_id])
            association_checks.append(
                bool(product_rows)
                and product_rows == configuration_rows
                and len(product_rows) == round_captures["product_metrics"].get("captured")
                and len(configuration_rows)
                == round_captures["promotion_configuration"].get("captured")
            )
            catalog_association_checks.append(
                bool(catalog_product_ids)
                and {platform_product_id for _, _, platform_product_id in product_rows}
                <= catalog_product_ids
            )

    expected_capture_count = args.captures_per_dataset * len(CURRENT_DATASETS) + 1
    capability_statuses = capabilities.get("datasets", {})
    capability_details = capabilities.get("dataset_details", {})
    product_detail = (
        capability_details.get("product_metrics", {})
        if isinstance(capability_details, dict)
        else {}
    )
    configuration_detail = (
        capability_details.get("promotion_configuration", {})
        if isinstance(capability_details, dict)
        else {}
    )
    campaign_detail = (
        capability_details.get("campaign_metrics", {})
        if isinstance(capability_details, dict)
        else {}
    )
    overview_detail = (
        capability_details.get("promotion_overview", {})
        if isinstance(capability_details, dict)
        else {}
    )
    capability_details_check = (
        overview_detail.get("status") == "REAL_PROMOTION_WINDOWS"
        and overview_detail.get("supported_window_kinds") == ["TODAY", "YESTERDAY"]
        and overview_detail.get("entity_granularity") == "ACCOUNT"
        and overview_detail.get("current_only") is False
        and overview_detail.get("verified") is True
        and product_detail.get("status") == "REAL_PROMOTED_PRODUCT_METRICS"
        and product_detail.get("supported_window_kinds") == ["TODAY"]
        and product_detail.get("entity_granularity") == "PROMOTED_PRODUCT"
        and product_detail.get("current_only") is False
        and product_detail.get("verified") is True
        and configuration_detail.get("status") == "REAL_PROMOTION_CONFIGURATION_CURRENT"
        and configuration_detail.get("supported_window_kinds") == ["POINT_IN_TIME"]
        and configuration_detail.get("entity_granularity") == "PROMOTED_PRODUCT"
        and configuration_detail.get("current_only") is True
        and configuration_detail.get("verified") is True
        and campaign_detail.get("status") == "UNAVAILABLE"
        and campaign_detail.get("supported_window_kinds") == []
        and campaign_detail.get("entity_granularity") == "CAMPAIGN"
        and campaign_detail.get("verified") is False
    )
    capture_snapshot_ids = {
        item["snapshot_id"] for item in captures if isinstance(item.get("snapshot_id"), str)
    }
    passed = (
        args.captures_per_dataset == 3
        and len(captures) == expected_capture_count
        and len(capture_snapshot_ids) == expected_capture_count
        and all(
            item.get("status") == "SUCCEEDED"
            and item.get("committed") is True
            and item.get("idempotent_replay") is False
            and item.get("coverage") == "COMPLETE"
            and item.get("truncated") is False
            for item in captures
        )
        and all(
            item.get("captured") == (1 if item["dataset_type"] == "promotion_overview" else 3)
            and item.get("total_observed")
            == (1 if item["dataset_type"] == "promotion_overview" else 3)
            for item in captures
        )
        and all(
            sum(item["dataset_type"] == dataset and item["round"] > 0 for item in captures) == 3
            for dataset in CURRENT_DATASETS
        )
        and sum(
            item["dataset_type"] == "promotion_overview" and item["window"] == "YESTERDAY"
            for item in captures
        )
        == 1
        and all(latest_checks)
        and len(latest_checks) == expected_capture_count
        and all(read_contract_checks)
        and len(read_contract_checks) == expected_capture_count
        and all(
            page_count == (1 if item["dataset_type"] == "promotion_overview" else 2)
            for item, page_count in zip(captures, read_page_counts, strict=True)
        )
        and len(read_page_counts) == expected_capture_count
        and set(list_checks) == set(LIST_DATASETS)
        and all(list_checks.values())
        and all(restart_readback)
        and len(restart_readback) == expected_capture_count
        and all(idempotent_replay)
        and len(idempotent_replay) == expected_capture_count
        and len(association_checks) == args.captures_per_dataset
        and all(association_checks)
        and catalog_contract_check
        and len(catalog_association_checks) == args.captures_per_dataset
        and all(catalog_association_checks)
        and set(tools_seen) == EXPECTED_TOOLS
        and connection_status.get("status") == "NOT_CHECKED"
        and connection_status.get("real_collection_enabled") is True
        and connection_status.get("synthetic_test_enabled") is False
        and isinstance(capability_statuses, dict)
        and all(
            capability_statuses.get(dataset) == expected
            for dataset, expected in EXPECTED_CAPABILITIES.items()
        )
        and capability_details_check
        and date_stability_check
        and negative_product_yesterday_check
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "tools": tools_seen,
        "capability_statuses": capability_statuses,
        "capability_details_check": capability_details_check,
        "connection_status": connection_status.get("status"),
        "run_business_date": run_business_date.isoformat(),
        "date_stability_check": date_stability_check,
        "negative_product_yesterday_check": negative_product_yesterday_check,
        "captures": captures,
        "latest_checks": latest_checks,
        "read_contract_checks": read_contract_checks,
        "read_page_counts": read_page_counts,
        "list_pagination_checks": list_checks,
        "restart_readback": restart_readback,
        "idempotent_replay": idempotent_replay,
        "product_configuration_associations": association_checks,
        "catalog_snapshot_id": catalog_snapshot_id,
        "catalog_contract_check": catalog_contract_check,
        "promoted_products_are_catalog_subset": catalog_association_checks,
        "raw_store_id_printed": False,
        "platform_entity_ids_printed": False,
        "product_names_printed": False,
        "metric_or_configuration_values_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded Stage D4 sync over MCP stdio.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--store-id", required=True)
    parser.add_argument("--captures-per-dataset", type=int, default=3, choices=(3,))
    parser.add_argument("--idempotency-prefix", default="")
    parser.add_argument("--limit", type=int, default=50, choices=range(1, 201))
    parser.add_argument("--wait-seconds", type=int, default=60, choices=range(60, 601))
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required for a real run")
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
