from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import Any

from conftest import write_test_config
from mcp import Client, StdioServerParameters


def content(result: Any) -> dict[str, Any]:
    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def params(config: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pdd_data_mcp.cli", "serve", "--config", str(config)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


async def exercise(config: Path) -> None:
    scope = {
        "kind": "POINT_IN_TIME",
        "business_date": date.today().isoformat(),
        "timezone": "Asia/Shanghai",
        "object_type": "ACCOUNT_ALL",
        "filters": {},
        "currency": "CNY",
        "attribution": "PLATFORM_DEFAULT",
        "version": "1",
        "start": None,
        "end": None,
    }
    async with Client(params(config), raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "pdd_get_capabilities",
            "pdd_get_connection_status",
            "pdd_collect_snapshot",
            "pdd_list_snapshots",
            "pdd_read_snapshot",
            "pdd_get_latest_snapshot",
        }
        collect_tool = next(tool for tool in tools.tools if tool.name == "pdd_collect_snapshot")
        assert collect_tool.annotations is not None
        assert collect_tool.annotations.read_only_hint is False
        capabilities = content(await client.call_tool("pdd_get_capabilities"))
        assert capabilities["real_collection"] == "DISABLED"
        collect = content(
            await client.call_tool(
                "pdd_collect_snapshot",
                {
                    "connection_id": "conn_01",
                    "dataset_type": "product_catalog",
                    "scope": scope,
                    "idempotency_key": "cross-process-key",
                    "batch_id": "batch_cross_process",
                    "limit": 50,
                },
            )
        )
        assert collect["status"] == "PARTIAL"
        assert collect["truncated"] is True
        snapshot_id = collect["snapshot_id"]
        assert collect["batch_id"] == "batch_cross_process"
        listed = content(
            await client.call_tool(
                "pdd_list_snapshots",
                {"store_id": "st_test_001", "dataset_type": "product_catalog", "limit": 1},
            )
        )
        assert listed["items"][0]["snapshot_id"] == snapshot_id
        read = content(
            await client.call_tool(
                "pdd_read_snapshot", {"snapshot_id": snapshot_id, "page_size": 25}
            )
        )
        assert len(read["records"]) == 25
    async with Client(params(config), raise_exceptions=True) as restarted:
        reread = content(
            await restarted.call_tool(
                "pdd_read_snapshot", {"snapshot_id": snapshot_id, "page_size": 25}
            )
        )
        replay = content(
            await restarted.call_tool(
                "pdd_collect_snapshot",
                {
                    "connection_id": "conn_01",
                    "dataset_type": "product_catalog",
                    "scope": scope,
                    "idempotency_key": "cross-process-key",
                    "batch_id": "batch_cross_process",
                    "limit": 50,
                },
            )
        )
        assert reread["snapshot_id"] == snapshot_id
        assert replay["snapshot_id"] == snapshot_id
        assert replay["idempotent_replay"] is True


def test_real_mcp_stdio_subprocess_restart_and_idempotency(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_test_config(config)
    asyncio.run(exercise(config))
