from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, Page, Response, async_playwright

from pdd_data_mcp.browser.promotion import sanitized_json_shape
from pdd_data_mcp.config import load_config

TARGET_PAGE_URL = "https://mms.pinduoduo.com/sycm/goods_effect"
REQUIRED_SAME_CONTEXT_PAGES = (
    "https://yingxiao.pinduoduo.com/mains/promotionOverview",
    "https://mms.pinduoduo.com/goods/goods_list",
    "https://mms.pinduoduo.com/home",
)
CANDIDATE_RESPONSE_HOST = "mms.pinduoduo.com"
CANDIDATE_RESPONSE_PATHS = (
    "/sydney/api/goodsDataShow/queryGoodsDetailVOListForMMS",
    "/sydney/api/goodsDataShow/queryGoodsPageOverView",
    "/sydney/api/goodsDataShow/queryGoodsPageOverViewReadyDate",
    "/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms",
    "/sydney/api/goodsDataShow/queryGoodsReadyDate",
)
MAX_RESPONSE_BYTES = 1_048_576
MAX_CANDIDATE_OBSERVATIONS = 50
OBSERVE_SECONDS = 8.0

_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_SHAPE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
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
    "CANDIDATE_OBSERVATION_LIMIT_EXCEEDED",
    "CONTENT_LENGTH_INVALID",
    "RESPONSE_TOO_LARGE",
    "REQUEST_JSON_UNAVAILABLE",
    "INVALID_REQUEST_JSON_VALUE",
    "DUPLICATE_JSON_KEY",
    "NON_FINITE_JSON_NUMBER",
    "INVALID_JSON_RESPONSE",
    "JSON_STRUCTURE_TOO_LARGE",
    "SHAPE_CAPTURE_FAILED",
    "PLAYWRIGHT_STOP_FAILED",
    "UNEXPECTED_FAILURE",
]

_GATE_SELECTORS: tuple[tuple[ErrorCode, tuple[str, ...]], ...] = (
    ("LOGIN_REQUIRED", _LOGIN_SELECTORS),
    ("CAPTCHA_PRESENT", _CAPTCHA_SELECTORS),
    ("RISK_CONTROL_PRESENT", _RISK_SELECTORS),
)


@dataclass(frozen=True)
class ShapeRuntime:
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
    response_events_seen: int = 0
    ignored_response_events: int = 0
    candidate_response_events: int = 0
    request_json_reads: int = 0
    response_body_reads: int = 0
    dom_business_content_reads: int = 0
    safety_selector_checks: int = 0
    listener_removals: int = 0
    scalar_value_outputs: int = 0
    identifier_outputs: int = 0
    name_outputs: int = 0
    date_value_outputs: int = 0
    header_outputs: int = 0
    raw_body_outputs: int = 0
    title_outputs: int = 0
    query_outputs: int = 0
    fragment_outputs: int = 0
    full_url_outputs: int = 0
    playwright_stop_attempts: int = 0


class ShapeStopped(RuntimeError):
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


def _load_runtime(config_path: Path, connection_id: str) -> ShapeRuntime:
    if _CONNECTION_ID.fullmatch(connection_id) is None:
        raise ShapeStopped("INVALID_CONNECTION_ID")
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise ShapeStopped("CONFIG_LOAD_FAILED") from exc
    try:
        connection = config.connection(connection_id)
    except KeyError as exc:
        raise ShapeStopped("CONNECTION_ID_NOT_FOUND") from exc
    if config.service.test_mode:
        raise ShapeStopped("REAL_MODE_REQUIRED")
    if not connection.real_collection_enabled:
        raise ShapeStopped("REAL_COLLECTION_NOT_ENABLED")
    if connection.cdp_endpoint is None:
        raise ShapeStopped("CDP_ENDPOINT_NOT_CONFIGURED")
    if not _is_trusted_loopback_endpoint(connection.cdp_endpoint):
        raise ShapeStopped("CDP_ENDPOINT_NOT_TRUSTED")
    return ShapeRuntime(
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


def _select_target_page(browser: Browser) -> tuple[Page, int, int, int]:
    contexts = list(browser.contexts)
    open_page_count = 0
    matches: list[tuple[Page, Any]] = []
    for context in contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            open_page_count += 1
            if _matches_exact_page(page.url, TARGET_PAGE_URL):
                matches.append((page, context))
    if not matches:
        raise ShapeStopped("TARGET_PAGE_NOT_FOUND")
    if len(matches) != 1:
        raise ShapeStopped("TARGET_PAGE_AMBIGUOUS")

    target, target_context = matches[0]
    for required_url in REQUIRED_SAME_CONTEXT_PAGES:
        required_matches = [
            page
            for page in target_context.pages
            if not page.is_closed() and _matches_exact_page(page.url, required_url)
        ]
        if not required_matches:
            raise ShapeStopped("REQUIRED_CONTEXT_PAGE_MISSING")
        if len(required_matches) != 1:
            raise ShapeStopped("REQUIRED_CONTEXT_PAGE_AMBIGUOUS")
    return target, len(contexts), open_page_count, len(REQUIRED_SAME_CONTEXT_PAGES)


async def _assert_safety_gate(page: Page, counts: SafetyCounts) -> None:
    for code, selectors in _GATE_SELECTORS:
        for selector in selectors:
            counts.safety_selector_checks += 1
            if await page.locator(selector).count() > 0:
                raise ShapeStopped(code, counts)


def _candidate_path(response: Response) -> str | None:
    try:
        parsed = urlsplit(response.url)
        port = parsed.port
    except ValueError:
        return None
    content_type = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if (
        parsed.scheme != "https"
        or parsed.hostname != CANDIDATE_RESPONSE_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in CANDIDATE_RESPONSE_PATHS
        or response.request.method != "POST"
        or response.status != 200
        or content_type != "application/json"
    ):
        return None
    return parsed.path


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ShapeStopped("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ShapeStopped("NON_FINITE_JSON_NUMBER")


def _strict_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except ShapeStopped:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ShapeStopped("INVALID_JSON_RESPONSE") from exc


def _validate_json_value(value: object, *, max_nodes: int = 20_000) -> None:
    remaining = max_nodes

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ShapeStopped("JSON_STRUCTURE_TOO_LARGE")
        if item is None or isinstance(item, str | bool | int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ShapeStopped("NON_FINITE_JSON_NUMBER")
            return
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ShapeStopped("NON_FINITE_JSON_NUMBER")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ShapeStopped("INVALID_REQUEST_JSON_VALUE")
                visit(child, depth + 1)
            return
        raise ShapeStopped("INVALID_REQUEST_JSON_VALUE")

    visit(value, 0)


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float | Decimal):
        return "number"
    return "other"


def _array_path_lengths(
    value: object, *, max_depth: int = 10, max_entries: int = 200
) -> list[dict[str, int | str]]:
    result: list[dict[str, int | str]] = []

    def visit(item: object, path: str, depth: int) -> None:
        if depth > max_depth or len(result) >= max_entries:
            return
        if isinstance(item, list):
            result.append({"path": path or "$", "length": len(item)})
            if item:
                visit(item[0], f"{path}[]" if path else "$[]", depth + 1)
            return
        if isinstance(item, dict):
            for key in sorted(item):
                safe_key = key if _SAFE_SHAPE_KEY.fullmatch(key) else "{key}"
                child_path = f"{path}.{safe_key}" if path else safe_key
                visit(item[key], child_path, depth + 1)

    visit(value, "", 0)
    return result


async def _probe_candidate(
    response: Response, path: str, counts: SafetyCounts
) -> dict[str, object]:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ShapeStopped("CONTENT_LENGTH_INVALID", counts) from exc
        if declared_length < 0:
            raise ShapeStopped("CONTENT_LENGTH_INVALID", counts)
        if declared_length > MAX_RESPONSE_BYTES:
            raise ShapeStopped("RESPONSE_TOO_LARGE", counts)

    counts.request_json_reads += 1
    try:
        request_payload = response.request.post_data_json
    except Error as exc:
        raise ShapeStopped("REQUEST_JSON_UNAVAILABLE", counts) from exc
    _validate_json_value(request_payload)

    counts.response_body_reads += 1
    raw = await response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ShapeStopped("RESPONSE_TOO_LARGE", counts)
    response_payload = _strict_json(raw)
    _validate_json_value(response_payload)

    return {
        "path": path,
        "request_type": _json_type(request_payload),
        "request_shape": sanitized_json_shape(
            request_payload,
            max_depth=10,
            max_entries=600,
        ),
        "request_array_lengths": _array_path_lengths(request_payload),
        "response_type": _json_type(response_payload),
        "response_shape": sanitized_json_shape(
            response_payload,
            max_depth=12,
            max_entries=1_000,
        ),
        "response_array_lengths": _array_path_lengths(response_payload, max_depth=12),
    }


def _aggregate_observations(observations: list[dict[str, object]]) -> list[dict[str, object]]:
    by_path: dict[str, Counter[str]] = {path: Counter() for path in CANDIDATE_RESPONSE_PATHS}
    variants: dict[str, dict[str, object]] = {}
    for observation in observations:
        path = str(observation["path"])
        safe_variant = {key: value for key, value in observation.items() if key != "path"}
        signature = json.dumps(
            safe_variant, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        variants[signature] = safe_variant
        by_path[path][signature] += 1

    return [
        {
            "path": path,
            "observation_count": sum(by_path[path].values()),
            "variants": [
                {**variants[signature], "observations": count}
                for signature, count in sorted(by_path[path].items())
            ],
        }
        for path in CANDIDATE_RESPONSE_PATHS
    ]


async def _capture_shapes(
    page: Page,
    *,
    collection_timeout_ms: int,
    observe_seconds: float,
    counts: SafetyCounts,
) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()
    failures: list[ShapeStopped] = []
    active = True

    async def consume(response: Response, path: str) -> None:
        try:
            observations.append(await _probe_candidate(response, path, counts))
        except ShapeStopped as exc:
            failures.append(exc)
        except Exception as exc:
            failures.append(ShapeStopped("SHAPE_CAPTURE_FAILED", counts))
            failures[-1].__cause__ = exc

    def on_response(response: Response) -> None:
        if not active:
            return
        counts.response_events_seen += 1
        path = _candidate_path(response)
        if path is None:
            counts.ignored_response_events += 1
            return
        counts.candidate_response_events += 1
        if counts.candidate_response_events > MAX_CANDIDATE_OBSERVATIONS:
            if not failures:
                failures.append(ShapeStopped("CANDIDATE_OBSERVATION_LIMIT_EXCEEDED", counts))
            return
        task = asyncio.create_task(consume(response, path))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

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
            raise ShapeStopped("TARGET_RELOAD_FAILED", counts) from exc
        await asyncio.sleep(observe_seconds)
        if tasks:
            await asyncio.gather(*list(tasks))
        if failures:
            raise failures[0]
        if not _matches_exact_page(page.url, TARGET_PAGE_URL):
            raise ShapeStopped("TARGET_PAGE_CHANGED", counts)
        await _assert_safety_gate(page, counts)
    finally:
        active = False
        page.remove_listener("response", on_response)
        counts.listener_removals += 1
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    return _aggregate_observations(observations)


async def shape_inventory(
    runtime: ShapeRuntime, *, observe_seconds: float = OBSERVE_SECONDS
) -> dict[str, object]:
    counts = SafetyCounts()
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise ShapeStopped("PLAYWRIGHT_START_FAILED", counts) from exc

    stopped: ShapeStopped | None = None
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
            raise ShapeStopped("CDP_CONNECTION_FAILED", counts) from exc
        try:
            target, context_count, open_page_count, required_page_count = _select_target_page(
                browser
            )
            results = await _capture_shapes(
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
                "candidate_path_count": len(CANDIDATE_RESPONSE_PATHS),
                "observed_candidate_path_count": sum(
                    cast(int, item["observation_count"]) > 0 for item in results
                ),
                "results": results,
            }
        except ShapeStopped:
            raise
        except Exception as exc:
            raise ShapeStopped("SHAPE_CAPTURE_FAILED", counts) from exc
    except ShapeStopped as exc:
        stopped = exc
    finally:
        counts.playwright_stop_attempts += 1
        try:
            await manager.stop()
        except Error as exc:
            raise ShapeStopped("PLAYWRIGHT_STOP_FAILED", counts) from exc

    if stopped is not None:
        stopped.counts = counts
        raise stopped
    assert result is not None
    result["safety_counts"] = asdict(counts)
    return result


def _error_payload(stopped: ShapeStopped) -> dict[str, object]:
    return {
        "status": "STOPPED",
        "error_code": stopped.code,
        "results": [],
        "safety_counts": asdict(stopped.counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read sanitized JSON shapes for five fixed product-business candidates."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    try:
        if not args.confirm_read_only:
            raise ShapeStopped("READ_ONLY_CONFIRMATION_REQUIRED")
        runtime = _load_runtime(args.config, args.connection_id)
        payload = asyncio.run(shape_inventory(runtime))
    except ShapeStopped as exc:
        payload = _error_payload(exc)
        exit_code = 1
    except Exception:
        payload = _error_payload(ShapeStopped("UNEXPECTED_FAILURE"))
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
