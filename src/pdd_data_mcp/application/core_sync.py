from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CORE_DATASETS = ("store_overview", "product_catalog", "inventory")


def core_sync_status(results: Mapping[str, Mapping[str, Any]]) -> str:
    succeeded = sum(bool(result.get("committed")) for result in results.values())
    if succeeded == len(CORE_DATASETS):
        return "SUCCESS"
    return "PARTIAL_SUCCESS" if succeeded else "FAILED"
