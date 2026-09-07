from __future__ import annotations

from pathlib import Path

import pytest

from pdd_data_mcp.config import AppConfig, load_config
from pdd_data_mcp.storage import LocalFileSnapshotRepository


def write_test_config(
    path: Path,
    *,
    max_bytes: int = 1_073_741_824,
    max_response_bytes: int = 262_144,
    stores: tuple[str, ...] = ("st_test_001",),
    test_mode: bool = True,
) -> AppConfig:
    data_root = path.parent / "data"
    runtime_root = path.parent / "runtime"
    connections = "\n".join(
        f'''[[connections]]
connection_id = "conn_{index:02d}"
store_id = "{store}"
cdp_endpoint = ""
expected_platform_store_id = ""
real_collection_enabled = false
synthetic_enabled = {str(test_mode).lower()}
'''
        for index, store in enumerate(stores, 1)
    )
    content = f'''[service]
name = "pdd-data-mcp"
transport = "stdio"
mode = "read_only"
test_mode = {str(test_mode).lower()}

[storage]
backend = "local_files"
data_root = "{data_root.as_posix()}"
runtime_root = "{runtime_root.as_posix()}"
partition_timezone = "Asia/Shanghai"
layout_version = "1"
max_bytes = {max_bytes}
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
max_mcp_response_bytes = {max_response_bytes}

{connections}
'''
    path.write_text(content, encoding="utf-8")
    return load_config(path)


def make_repository(
    config: AppConfig, *, allowed: frozenset[str] | None = None
) -> LocalFileSnapshotRepository:
    return LocalFileSnapshotRepository(
        config.storage,
        allowed_store_ids=allowed if allowed is not None else config.allowed_store_ids,
        max_response_bytes=config.collection.max_mcp_response_bytes,
    )


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return write_test_config(tmp_path / "config.toml")
