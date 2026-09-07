from __future__ import annotations

import sys
from pathlib import Path

from pdd_data_mcp.config import load_config
from pdd_data_mcp.storage import LocalFileSnapshotRepository


def main() -> None:
    config = load_config(Path(sys.argv[1]))
    repository = LocalFileSnapshotRepository(
        config.storage,
        allowed_store_ids=config.allowed_store_ids,
        max_response_bytes=config.collection.max_mcp_response_bytes,
    )
    with repository.service_lock():
        repository.initialize()
        print("READY", flush=True)
        input()


if __name__ == "__main__":
    main()
