from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp import Client, StdioServerParameters

from pdd_data_mcp.application.core_sync import CORE_DATASETS, core_sync_status

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


def scope(dataset: str) -> dict[str, Any]:
    return {
        "kind": "TODAY" if dataset == "store_overview" else "POINT_IN_TIME",
        "business_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "timezone": "Asia/Shanghai",
        "object_type": "ACCOUNT_ALL",
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": "1",
        "start": None,
        "end": None,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    batch_id = f"batch_{uuid.uuid4().hex}"
    key_prefix = args.idempotency_prefix or f"core-{uuid.uuid4().hex}"
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    reads: dict[str, dict[str, Any]] = {}
    tool_count = 0
    async with Client(parameters(args.config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        tool_count = len(tools.tools)
        for index, dataset in enumerate(CORE_DATASETS):
            call = {
                "connection_id": args.connection_id,
                "dataset_type": dataset,
                "scope": scope(dataset),
                "idempotency_key": f"{key_prefix}-{dataset}",
                "batch_id": batch_id,
                "limit": args.limit,
            }
            calls[dataset] = call
            result = structured(await client.call_tool("pdd_collect_snapshot", call))
            results[dataset] = result
            if result.get("committed"):
                reads[dataset] = structured(
                    await client.call_tool(
                        "pdd_read_snapshot",
                        {"snapshot_id": result["snapshot_id"], "page_size": 200},
                    )
                )
            if not result.get("committed") or result.get("status") in STOP_STATUSES:
                break
            if index < len(CORE_DATASETS) - 1 and args.wait_seconds:
                await asyncio.sleep(args.wait_seconds)

    restart_readback: dict[str, bool] = {}
    idempotent_replay: dict[str, bool] = {}
    async with Client(parameters(args.config), raise_exceptions=True) as restarted:
        for dataset, result in results.items():
            if not result.get("committed"):
                continue
            snapshot_id = result["snapshot_id"]
            read = structured(
                await restarted.call_tool("pdd_read_snapshot", {"snapshot_id": snapshot_id})
            )
            replay = structured(await restarted.call_tool("pdd_collect_snapshot", calls[dataset]))
            restart_readback[dataset] = read["snapshot_id"] == snapshot_id
            idempotent_replay[dataset] = (
                replay["snapshot_id"] == snapshot_id and replay["idempotent_replay"] is True
            )

    product_ids = {
        row["platform_product_id"] for row in reads.get("product_catalog", {}).get("records", [])
    }
    inventory_ids = {
        row["platform_product_id"] for row in reads.get("inventory", {}).get("records", [])
    }
    mapping_valid = bool(product_ids) and inventory_ids.issubset(product_ids)
    public_results = {
        dataset: {
            "status": result["status"],
            "committed": result["committed"],
            "snapshot_id": result.get("snapshot_id"),
            "captured": result.get("captured"),
            "total_observed": result.get("total_observed"),
            "coverage": result.get("coverage"),
            "truncated": result.get("truncated"),
        }
        for dataset, result in results.items()
    }
    return {
        "status": core_sync_status(results),
        "batch_id": batch_id,
        "datasets": public_results,
        "product_inventory_mapping": mapping_valid,
        "restart_readback": restart_readback,
        "idempotent_replay": idempotent_replay,
        "tool_count": tool_count,
        "raw_store_id_printed": False,
        "product_business_fields_printed": False,
        "store_metric_values_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded D1/D2/D3 sync over MCP stdio.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--idempotency-prefix", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required for a real run")
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "FAILED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
