from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, async_playwright

from pdd_data_mcp.browser.promotion import sanitize_discovery_path
from pdd_data_mcp.config import load_config

_ALLOWED_PAGE_HOSTS = frozenset(
    {
        "mms.pinduoduo.com",
        "yingxiao.pinduoduo.com",
    }
)
_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

type ErrorCode = Literal[
    "INVALID_CONNECTION_ID",
    "CONFIG_LOAD_FAILED",
    "CONNECTION_ID_NOT_FOUND",
    "REAL_MODE_REQUIRED",
    "REAL_COLLECTION_NOT_ENABLED",
    "CDP_ENDPOINT_NOT_CONFIGURED",
    "CDP_ENDPOINT_NOT_TRUSTED",
    "PLAYWRIGHT_START_FAILED",
    "CDP_CONNECTION_FAILED",
    "PAGE_INVENTORY_FAILED",
    "PLAYWRIGHT_STOP_FAILED",
    "UNEXPECTED_FAILURE",
]


class InventoryStopped(RuntimeError):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InventoryRuntime:
    cdp_endpoint: str
    connect_timeout_ms: int


def _is_trusted_loopback_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "ws"}
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and port is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _load_runtime(config_path: Path, connection_id: str) -> InventoryRuntime:
    if _CONNECTION_ID.fullmatch(connection_id) is None:
        raise InventoryStopped("INVALID_CONNECTION_ID")
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise InventoryStopped("CONFIG_LOAD_FAILED") from exc
    try:
        connection = config.connection(connection_id)
    except KeyError as exc:
        raise InventoryStopped("CONNECTION_ID_NOT_FOUND") from exc
    if config.service.test_mode:
        raise InventoryStopped("REAL_MODE_REQUIRED")
    if not connection.real_collection_enabled:
        raise InventoryStopped("REAL_COLLECTION_NOT_ENABLED")
    if connection.cdp_endpoint is None:
        raise InventoryStopped("CDP_ENDPOINT_NOT_CONFIGURED")
    if not _is_trusted_loopback_endpoint(connection.cdp_endpoint):
        raise InventoryStopped("CDP_ENDPOINT_NOT_TRUSTED")
    return InventoryRuntime(
        cdp_endpoint=connection.cdp_endpoint,
        connect_timeout_ms=config.collection.connect_timeout_ms,
    )


def _safe_page_reference(raw_url: str) -> dict[str, int | str] | None:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_PAGE_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return {
        "host": host,
        "path": sanitize_discovery_path(parsed.path or "/"),
    }


async def _inventory_open_pages(browser: Browser) -> dict[str, object]:
    contexts = list(browser.contexts)
    pages: list[dict[str, int | str]] = []
    page_slot_count = 0
    open_page_count = 0
    closed_page_count = 0
    excluded_open_page_count = 0

    for context_index, context in enumerate(contexts):
        for page_index, page in enumerate(context.pages):
            page_slot_count += 1
            if page.is_closed():
                closed_page_count += 1
                continue
            open_page_count += 1
            reference = _safe_page_reference(page.url)
            if reference is None:
                excluded_open_page_count += 1
                continue
            pages.append(
                {
                    "context_index": context_index,
                    "page_index": page_index,
                    **reference,
                }
            )

    return {
        "status": "PASS",
        "error_code": None,
        "context_count": len(contexts),
        "page_slot_count": page_slot_count,
        "open_page_count": open_page_count,
        "closed_page_count": closed_page_count,
        "allowed_pdd_page_count": len(pages),
        "excluded_open_page_count": excluded_open_page_count,
        "pages": pages,
        "safety_counts": {
            "reload_actions": 0,
            "navigation_actions": 0,
            "click_actions": 0,
            "page_close_actions": 0,
            "dom_reads": 0,
            "response_body_reads": 0,
            "title_outputs": 0,
            "query_outputs": 0,
            "fragment_outputs": 0,
            "full_url_outputs": 0,
        },
    }


async def inventory(runtime: InventoryRuntime) -> dict[str, object]:
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise InventoryStopped("PLAYWRIGHT_START_FAILED") from exc

    stopped: InventoryStopped | None = None
    result: dict[str, object] | None = None
    try:
        try:
            browser = await manager.chromium.connect_over_cdp(
                runtime.cdp_endpoint,
                timeout=runtime.connect_timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            raise InventoryStopped("CDP_CONNECTION_FAILED") from exc
        try:
            result = await _inventory_open_pages(browser)
        except Error as exc:
            raise InventoryStopped("PAGE_INVENTORY_FAILED") from exc
    except InventoryStopped as exc:
        stopped = exc
    finally:
        try:
            await manager.stop()
        except Error as exc:
            raise InventoryStopped("PLAYWRIGHT_STOP_FAILED") from exc

    if stopped is not None:
        raise stopped
    assert result is not None
    return result


def _error_payload(code: ErrorCode) -> dict[str, object]:
    return {
        "status": "STOPPED",
        "error_code": code,
        "pages": [],
        "safety_counts": {
            "reload_actions": 0,
            "navigation_actions": 0,
            "click_actions": 0,
            "page_close_actions": 0,
            "dom_reads": 0,
            "response_body_reads": 0,
            "title_outputs": 0,
            "query_outputs": 0,
            "fragment_outputs": 0,
            "full_url_outputs": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory sanitized paths of already-open PDD pages without page mutation."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    args = parser.parse_args()
    try:
        runtime = _load_runtime(args.config, args.connection_id)
        payload = asyncio.run(inventory(runtime))
    except InventoryStopped as exc:
        payload = _error_payload(exc.code)
        exit_code = 1
    except Exception:
        payload = _error_payload("UNEXPECTED_FAILURE")
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
