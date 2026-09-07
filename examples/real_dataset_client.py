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
    batch_id = args.batch_id or f"batch_{uuid.uuid4().hex}"
    call = {
        "connection_id": args.connection_id,
        "dataset_type": args.dataset,
        "scope": scope(args.dataset),
        "idempotency_key": args.idempotency_key,
        "batch_id": batch_id,
        "limit": args.limit,
    }
    async with Client(parameters(args.config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        result = structured(await client.call_tool("pdd_collect_snapshot", call))
        if not result.get("committed"):
            return {
                "status": result.get("status"),
                "committed": False,
                "error_code": result.get("error_code"),
                "warnings": result.get("warnings"),
                "tool_count": len(tools.tools),
            }
        snapshot_id = result["snapshot_id"]
        read = structured(
            await client.call_tool(
                "pdd_read_snapshot", {"snapshot_id": snapshot_id, "page_size": 200}
            )
        )
    return {
        "status": "PASS",
        "committed": True,
        "dataset_type": args.dataset,
        "snapshot_id": snapshot_id,
        "batch_id": batch_id,
        "captured": result.get("captured"),
        "total_observed": result.get("total_observed"),
        "coverage": result.get("coverage"),
        "truncated": result.get("truncated"),
        "capture_method": read["manifest"]["capture_method"],
        "source": read["manifest"]["source"],
        "tool_count": len(tools.tools),
        "raw_store_id_printed": False,
        "product_ids_printed": False,
        "product_names_printed": False,
        "store_metric_values_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="One bounded D1/D2/D3 read over MCP stdio.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument(
        "--dataset", choices=("store_overview", "product_catalog", "inventory"), required=True
    )
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--limit", type=int, default=50)
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
