from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters


def _write_config(path: Path, output_root: Path) -> None:
    data_root = (output_root / "data").resolve()
    runtime_root = (output_root / "runtime").resolve()
    text = f'''[service]
name = "pdd-data-mcp"
transport = "stdio"
mode = "read_only"
test_mode = true

[storage]
backend = "local_files"
data_root = "{data_root.as_posix()}"
runtime_root = "{runtime_root.as_posix()}"
partition_timezone = "Asia/Shanghai"
layout_version = "1"
max_bytes = 1073741824
min_free_bytes = 0
retain_committed_snapshots = true
raw_response_persistence = false

[collection]
max_concurrency = 1
max_products = 50
connect_timeout_ms = 10000
collection_timeout_ms = 90000
min_interval_seconds = 0
platform_auto_retries = 0
max_mcp_response_bytes = 262144

[[connections]]
connection_id = "conn_synthetic_01"
store_id = "st_synthetic_001"
cdp_endpoint = ""
expected_platform_store_id = ""
real_collection_enabled = false
synthetic_enabled = true
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parameters(config: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pdd_data_mcp.cli", "serve", "--config", str(config)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def _result(value: Any) -> dict[str, Any]:
    if value.is_error:
        raise RuntimeError(f"MCP tool failed: {value.content!r}")
    if not isinstance(value.structured_content, dict):
        raise RuntimeError("MCP tool did not return structured content")
    return value.structured_content


async def _first_process(config: Path) -> dict[str, Any]:
    scope = {
        "kind": "POINT_IN_TIME",
        "business_date": datetime.now().date().isoformat(),
        "timezone": "Asia/Shanghai",
        "object_type": "ACCOUNT_ALL",
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": "1",
        "start": None,
        "end": None,
    }
    async with Client(_parameters(config), raise_exceptions=True) as client:
        discovered = await client.list_tools()
        tool_names = sorted(tool.name for tool in discovered.tools)
        collect = _result(
            await client.call_tool(
                "pdd_collect_snapshot",
                {
                    "connection_id": "conn_synthetic_01",
                    "dataset_type": "product_catalog",
                    "scope": scope,
                    "idempotency_key": "demo-ab-stable-key",
                    "limit": 50,
                },
            )
        )
        snapshot_id = str(collect["snapshot_id"])
        listed = _result(
            await client.call_tool(
                "pdd_list_snapshots",
                {
                    "store_id": "st_synthetic_001",
                    "dataset_type": "product_catalog",
                    "limit": 10,
                },
            )
        )
        read = _result(
            await client.call_tool(
                "pdd_read_snapshot", {"snapshot_id": snapshot_id, "page_size": 20}
            )
        )
    return {
        "tool_names": tool_names,
        "collect": collect,
        "list": listed,
        "first_read": read,
        "scope": scope,
        "snapshot_id": snapshot_id,
    }


async def _second_process(config: Path, first: dict[str, Any]) -> dict[str, Any]:
    async with Client(_parameters(config), raise_exceptions=True) as client:
        reread = _result(
            await client.call_tool(
                "pdd_read_snapshot",
                {"snapshot_id": first["snapshot_id"], "page_size": 20},
            )
        )
        replay = _result(
            await client.call_tool(
                "pdd_collect_snapshot",
                {
                    "connection_id": "conn_synthetic_01",
                    "dataset_type": "product_catalog",
                    "scope": first["scope"],
                    "idempotency_key": "demo-ab-stable-key",
                    "limit": 50,
                },
            )
        )
    return {"restart_read": reread, "idempotent_replay": replay}


async def run(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    config = output_root / "demo-config.toml"
    _write_config(config, output_root)
    first = await _first_process(config)
    second = await _second_process(config, first)
    passed = (
        len(first["tool_names"]) == 6
        and first["collect"]["committed"] is True
        and first["collect"]["snapshot_id"] == second["restart_read"]["snapshot_id"]
        and second["idempotent_replay"]["idempotent_replay"] is True
        and second["idempotent_replay"]["snapshot_id"] == first["snapshot_id"]
    )
    result = {
        "status": "PASS" if passed else "FAIL",
        "transport": "stdio",
        "processes_started": 2,
        "synthetic_source": first["first_read"]["manifest"]["source"],
        **first,
        **second,
    }
    (output_root / "demo-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "demo-output" / "acceptance",
    )
    args = parser.parse_args()
    result = asyncio.run(run(args.output.resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
