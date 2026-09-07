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


def parameters(config: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pdd_data_mcp.cli", "serve", "--config", str(config)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def structured(result: Any) -> dict[str, Any]:
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError(f"MCP call failed: {result.content!r}")
    return result.structured_content


async def run(args: argparse.Namespace) -> dict[str, Any]:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    scope = {
        "kind": "TODAY",
        "business_date": today,
        "timezone": "Asia/Shanghai",
        "object_type": "ACCOUNT_ALL",
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": "1",
        "start": None,
        "end": None,
    }
    call = {
        "connection_id": args.connection_id,
        "dataset_type": "promotion_overview",
        "scope": scope,
        "limit": 50,
        "idempotency_key": args.idempotency_key,
    }
    async with Client(parameters(args.config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        collection = structured(await client.call_tool("pdd_collect_snapshot", call))
        if not collection["committed"]:
            return {
                "status": collection["status"],
                "committed": False,
                "error_code": collection["error_code"],
                "warnings": collection["warnings"],
                "tool_count": len(tools.tools),
            }
        snapshot_id = str(collection["snapshot_id"])
        first_read = structured(
            await client.call_tool("pdd_read_snapshot", {"snapshot_id": snapshot_id})
        )
    async with Client(parameters(args.config), raise_exceptions=True) as restarted:
        restart_read = structured(
            await restarted.call_tool("pdd_read_snapshot", {"snapshot_id": snapshot_id})
        )
        replay = structured(await restarted.call_tool("pdd_collect_snapshot", call))
    result: dict[str, Any] = {
        "status": "PASS",
        "committed": True,
        "snapshot_id": snapshot_id,
        "source": first_read["manifest"]["source"],
        "capture_method": first_read["manifest"]["capture_method"],
        "business_scope": scope,
        "restart_readback": restart_read["snapshot_id"] == snapshot_id,
        "idempotent_replay": replay["snapshot_id"] == snapshot_id
        and replay["idempotent_replay"] is True,
        "tool_count": len(tools.tools),
    }
    if args.show_local_value:
        result["local_ad_spend_cents"] = first_read["data"]["metrics"]["ad_spend"]["value"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One bounded real promotion TODAY read through an MCP subprocess."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--confirm-read-only", action="store_true")
    parser.add_argument("--show-local-value", action="store_true")
    args = parser.parse_args()
    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required after the user authorizes this exact run")
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
