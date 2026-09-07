from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp import Client, StdioServerParameters

DATASETS = ("store_overview", "product_catalog", "inventory")


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
    results: dict[str, dict[str, object]] = {}
    async with Client(parameters(args.config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        for dataset in DATASETS:
            cursor: str | None = None
            listed_ids: list[str] = []
            pages = 0
            while True:
                listed = structured(
                    await client.call_tool(
                        "pdd_list_snapshots",
                        {
                            "store_id": args.store_id,
                            "dataset_type": dataset,
                            "cursor": cursor,
                            "limit": 2,
                        },
                    )
                )
                pages += 1
                listed_ids.extend(item["snapshot_id"] for item in listed["items"])
                cursor = listed.get("next_cursor")
                if not cursor:
                    break
            latest = structured(
                await client.call_tool(
                    "pdd_get_latest_snapshot",
                    {
                        "store_id": args.store_id,
                        "dataset_type": dataset,
                        "scope": scope(dataset),
                        "require_complete": True,
                    },
                )
            )
            if latest.get("status") != "FOUND" or not isinstance(latest.get("snapshot"), dict):
                raise RuntimeError(f"latest snapshot is unavailable for {dataset}")
            snapshot_id = latest["snapshot"]["snapshot_id"]
            read = structured(
                await client.call_tool(
                    "pdd_read_snapshot", {"snapshot_id": snapshot_id, "page_size": 200}
                )
            )
            results[dataset] = {
                "snapshot_count": len(listed_ids),
                "list_pages": pages,
                "unique_list_ids": len(listed_ids) == len(set(listed_ids)),
                "latest_snapshot_id": snapshot_id,
                "latest_in_list": snapshot_id in listed_ids,
                "readback_matches": read["snapshot_id"] == snapshot_id,
            }
    return {
        "status": "PASS",
        "tool_count": len(tools.tools),
        "datasets": results,
        "business_values_output": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read D1/D2/D3 history over MCP stdio.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--store-id", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
