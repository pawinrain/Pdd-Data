from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, Page, Response, async_playwright

from pdd_data_mcp.browser.promotion import sanitize_discovery_path
from pdd_data_mcp.config import load_config

TARGET_PAGE_URL = "https://mms.pinduoduo.com/sycm/goods_effect"
REQUIRED_SAME_CONTEXT_PAGES = (
    "https://yingxiao.pinduoduo.com/mains/promotionOverview",
    "https://mms.pinduoduo.com/goods/goods_list",
    "https://mms.pinduoduo.com/home",
)
MAX_METADATA_OBSERVATIONS = 300
OBSERVE_SECONDS = 8.0

_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_METHOD = re.compile(r"^[A-Z]{3,10}$")
_SAFE_CONTENT_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_LOGIN_SELECTORS = (
    'input[type="password"]',
    'form[action*="login"]',
    '[class*="LoginPage"]',
    '[class*="login-page"]',
)
_CAPTCHA_SELECTORS = (
    'input[placeholder*="验证码"]',
    'iframe[src*="captcha"]',
    '[class*="Captcha"]',
    '[class*="captcha"]',
)
_RISK_SELECTORS = (
    '[class*="RiskControl"]',
    '[class*="risk-control"]',
    '[data-testid*="risk"]',
    'iframe[src*="verify"]',
)

type ErrorCode = Literal[
    "READ_ONLY_CONFIRMATION_REQUIRED",
    "INVALID_CONNECTION_ID",
    "CONFIG_LOAD_FAILED",
    "CONNECTION_ID_NOT_FOUND",
    "REAL_MODE_REQUIRED",
    "REAL_COLLECTION_NOT_ENABLED",
    "CDP_ENDPOINT_NOT_CONFIGURED",
    "CDP_ENDPOINT_NOT_TRUSTED",
    "PLAYWRIGHT_START_FAILED",
    "CDP_CONNECTION_FAILED",
    "TARGET_PAGE_NOT_FOUND",
    "TARGET_PAGE_AMBIGUOUS",
    "REQUIRED_CONTEXT_PAGE_MISSING",
    "REQUIRED_CONTEXT_PAGE_AMBIGUOUS",
    "LOGIN_REQUIRED",
    "CAPTCHA_PRESENT",
    "RISK_CONTROL_PRESENT",
    "TARGET_RELOAD_FAILED",
    "TARGET_PAGE_CHANGED",
    "METADATA_CAPTURE_FAILED",
    "PLAYWRIGHT_STOP_FAILED",
    "UNEXPECTED_FAILURE",
]


@dataclass(frozen=True)
class MetadataRuntime:
    cdp_endpoint: str
    connect_timeout_ms: int
    collection_timeout_ms: int


@dataclass
class SafetyCounts:
    reload_actions: int = 0
    navigation_actions: int = 0
    click_actions: int = 0
    page_close_actions: int = 0
    browser_close_actions: int = 0
    response_body_reads: int = 0
    dom_business_content_reads: int = 0
    safety_selector_checks: int = 0
    title_outputs: int = 0
    query_outputs: int = 0
    fragment_outputs: int = 0
    full_url_outputs: int = 0
    arbitrary_header_outputs: int = 0
    playwright_stop_attempts: int = 0


class MetadataStopped(RuntimeError):
    def __init__(self, code: ErrorCode, counts: SafetyCounts | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.counts = counts or SafetyCounts()


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


def _load_runtime(config_path: Path, connection_id: str) -> MetadataRuntime:
    if _CONNECTION_ID.fullmatch(connection_id) is None:
        raise MetadataStopped("INVALID_CONNECTION_ID")
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise MetadataStopped("CONFIG_LOAD_FAILED") from exc
    try:
        connection = config.connection(connection_id)
    except KeyError as exc:
        raise MetadataStopped("CONNECTION_ID_NOT_FOUND") from exc
    if config.service.test_mode:
        raise MetadataStopped("REAL_MODE_REQUIRED")
    if not connection.real_collection_enabled:
        raise MetadataStopped("REAL_COLLECTION_NOT_ENABLED")
    if connection.cdp_endpoint is None:
        raise MetadataStopped("CDP_ENDPOINT_NOT_CONFIGURED")
    if not _is_trusted_loopback_endpoint(connection.cdp_endpoint):
        raise MetadataStopped("CDP_ENDPOINT_NOT_TRUSTED")
    return MetadataRuntime(
        cdp_endpoint=connection.cdp_endpoint,
        connect_timeout_ms=config.collection.connect_timeout_ms,
        collection_timeout_ms=config.collection.collection_timeout_ms,
    )


def _matches_exact_page(raw_url: str, expected_url: str) -> bool:
    try:
        actual = urlsplit(raw_url)
        expected = urlsplit(expected_url)
        actual_port = actual.port
    except ValueError:
        return False
    return (
        actual.scheme == "https"
        and actual.hostname == expected.hostname
        and actual_port in {None, 443}
        and actual.username is None
        and actual.password is None
        and actual.path.rstrip("/") == expected.path.rstrip("/")
    )


def _is_strict_pdd_https_url(raw_url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or not (host == "pinduoduo.com" or host.endswith(".pinduoduo.com"))
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return host, sanitize_discovery_path(parsed.path or "/")


def _response_metadata(response: Response) -> tuple[str, str, str, int, str] | None:
    safe_url = _is_strict_pdd_https_url(response.url)
    method = response.request.method.upper()
    status = response.status
    if safe_url is None or _SAFE_METHOD.fullmatch(method) is None:
        return None
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return None
    raw_content_type = response.headers.get("content-type", "")
    content_type = raw_content_type.partition(";")[0].strip().casefold()
    if _SAFE_CONTENT_TYPE.fullmatch(content_type) is None:
        content_type = "unknown"
    host, path = safe_url
    return host, path, method, status, content_type


def _select_target_page(browser: Browser) -> tuple[Page, int, int, int]:
    contexts = list(browser.contexts)
    open_page_count = 0
    matches: list[tuple[Page, object]] = []
    for context in contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            open_page_count += 1
            if _matches_exact_page(page.url, TARGET_PAGE_URL):
                matches.append((page, context))
    if not matches:
        raise MetadataStopped("TARGET_PAGE_NOT_FOUND")
    if len(matches) != 1:
        raise MetadataStopped("TARGET_PAGE_AMBIGUOUS")

    target, target_context = matches[0]
    for required_url in REQUIRED_SAME_CONTEXT_PAGES:
        required_matches = [
            page
            for page in target_context.pages  # type: ignore[attr-defined]
            if not page.is_closed() and _matches_exact_page(page.url, required_url)
        ]
        if not required_matches:
            raise MetadataStopped("REQUIRED_CONTEXT_PAGE_MISSING")
        if len(required_matches) != 1:
            raise MetadataStopped("REQUIRED_CONTEXT_PAGE_AMBIGUOUS")
    return target, len(contexts), open_page_count, len(REQUIRED_SAME_CONTEXT_PAGES)


async def _assert_safety_gate(page: Page, counts: SafetyCounts) -> None:
    for code, selectors in (
        ("LOGIN_REQUIRED", _LOGIN_SELECTORS),
        ("CAPTCHA_PRESENT", _CAPTCHA_SELECTORS),
        ("RISK_CONTROL_PRESENT", _RISK_SELECTORS),
    ):
        for selector in selectors:
            counts.safety_selector_checks += 1
            if await page.locator(selector).count() > 0:
                raise MetadataStopped(code, counts)  # type: ignore[arg-type]


async def _capture_metadata(
    page: Page,
    *,
    collection_timeout_ms: int,
    observe_seconds: float,
    counts: SafetyCounts,
) -> dict[str, object]:
    observations: Counter[tuple[str, str, str, int, str]] = Counter()
    response_events_seen = 0
    recorded_observation_count = 0
    ignored_response_count = 0
    capped_response_count = 0

    def on_response(response: Response) -> None:
        nonlocal response_events_seen
        nonlocal recorded_observation_count
        nonlocal ignored_response_count
        nonlocal capped_response_count
        response_events_seen += 1
        metadata = _response_metadata(response)
        if metadata is None:
            ignored_response_count += 1
            return
        if recorded_observation_count >= MAX_METADATA_OBSERVATIONS:
            capped_response_count += 1
            return
        recorded_observation_count += 1
        observations[metadata] += 1

    await _assert_safety_gate(page, counts)
    page.on("response", on_response)
    try:
        counts.reload_actions += 1
        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=collection_timeout_ms,
            )
        except Error as exc:
            raise MetadataStopped("TARGET_RELOAD_FAILED", counts) from exc
        await asyncio.sleep(observe_seconds)
        if not _matches_exact_page(page.url, TARGET_PAGE_URL):
            raise MetadataStopped("TARGET_PAGE_CHANGED", counts)
        await _assert_safety_gate(page, counts)
    finally:
        page.remove_listener("response", on_response)

    entries = [
        {
            "host": host,
            "path": path,
            "method": method,
            "status": status,
            "content_type": content_type,
            "observations": observation_count,
        }
        for (host, path, method, status, content_type), observation_count in sorted(
            observations.items()
        )
    ]
    return {
        "response_events_seen": response_events_seen,
        "recorded_observation_count": recorded_observation_count,
        "ignored_response_count": ignored_response_count,
        "capped_response_count": capped_response_count,
        "unique_metadata_count": len(entries),
        "entries": entries,
    }


async def metadata_inventory(
    runtime: MetadataRuntime, *, observe_seconds: float = OBSERVE_SECONDS
) -> dict[str, object]:
    counts = SafetyCounts()
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise MetadataStopped("PLAYWRIGHT_START_FAILED", counts) from exc

    stopped: MetadataStopped | None = None
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
            raise MetadataStopped("CDP_CONNECTION_FAILED", counts) from exc
        try:
            target, context_count, open_page_count, required_page_count = _select_target_page(
                browser
            )
            captured = await _capture_metadata(
                target,
                collection_timeout_ms=runtime.collection_timeout_ms,
                observe_seconds=observe_seconds,
                counts=counts,
            )
            result = {
                "status": "PASS",
                "error_code": None,
                "target_path": "/sycm/goods_effect",
                "context_count": context_count,
                "open_page_count": open_page_count,
                "target_page_count": 1,
                "required_same_context_page_count": required_page_count,
                **captured,
            }
        except MetadataStopped:
            raise
        except Exception as exc:
            raise MetadataStopped("METADATA_CAPTURE_FAILED", counts) from exc
    except MetadataStopped as exc:
        stopped = exc
    finally:
        counts.playwright_stop_attempts += 1
        try:
            await manager.stop()
        except Error as exc:
            raise MetadataStopped("PLAYWRIGHT_STOP_FAILED", counts) from exc

    if stopped is not None:
        stopped.counts = counts
        raise stopped
    assert result is not None
    result["safety_counts"] = asdict(counts)
    return result


def _error_payload(stopped: MetadataStopped) -> dict[str, object]:
    return {
        "status": "STOPPED",
        "error_code": stopped.code,
        "entries": [],
        "safety_counts": asdict(stopped.counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture sanitized response metadata from one fixed, already-open page."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()

    try:
        if not args.confirm_read_only:
            raise MetadataStopped("READ_ONLY_CONFIRMATION_REQUIRED")
        runtime = _load_runtime(args.config, args.connection_id)
        payload = asyncio.run(metadata_inventory(runtime))
    except MetadataStopped as exc:
        payload = _error_payload(exc)
        exit_code = 1
    except Exception:
        payload = _error_payload(MetadataStopped("UNEXPECTED_FAILURE"))
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
