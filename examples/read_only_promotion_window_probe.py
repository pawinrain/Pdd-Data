from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, Locator, Page, Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url
from pdd_data_mcp.browser.parsing import verify_store_identity
from pdd_data_mcp.errors import CollectionRejected

CDP_ENDPOINT = "http://127.0.0.1:9222"
PROMOTION_PAGE_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
IDENTITY_PAGE_URL = "https://mms.pinduoduo.com/mallcenter/info/basic"
PROMOTION_RESPONSE_HOST = "yingxiao.pinduoduo.com"
PROMOTION_RESPONSE_PATH = "/mms-gateway/venus/api/goods/promotion/v3/list"
IDENTITY_RESPONSE_HOST = "mms.pinduoduo.com"
IDENTITY_RESPONSE_PATH = "/earth/api/merchant/queryMerchantInfoByMallId"

QUICK_OPTIONS = tuple(f"DateAreaQuickOption_{index}" for index in range(5))
TODAY_OPTION = QUICK_OPTIONS[0]
MIN_ACTION_INTERVAL_SECONDS = 60.0
RESPONSE_TIMEOUT_MS = 90_000
MAX_RESPONSE_BYTES = 1_048_576

AUTH_SELECTORS = (
    'input[type="password"]',
    'input[placeholder*="验证码"]',
    'iframe[src*="captcha"]',
    '[class*="Captcha"]',
    '[class*="captcha"]',
)
STOP_PHRASES = (
    "请先登录",
    "登录已失效",
    "重新登录",
    "请输入验证码",
    "拖动滑块",
    "安全验证",
    "风险提示",
    "访问受限",
    "操作频繁",
    "账号异常",
)


class ProbeStopped(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _exact_response(host: str, path: str) -> Callable[[Response], bool]:
    def matches(response: Response) -> bool:
        parsed = urlsplit(response.url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == host
            and parsed.port is None
            and parsed.path == path
            and response.request.method == "POST"
            and response.status == 200
            and response.headers.get("content-type", "").partition(";")[0].casefold()
            == "application/json"
        )

    return matches


PROMOTION_RESPONSE_MATCHER = _exact_response(PROMOTION_RESPONSE_HOST, PROMOTION_RESPONSE_PATH)
IDENTITY_RESPONSE_MATCHER = _exact_response(IDENTITY_RESPONSE_HOST, IDENTITY_RESPONSE_PATH)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProbeStopped("DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ProbeStopped("NON_FINITE_JSON_NUMBER")


async def _read_json_object(response: Response) -> dict[str, Any]:
    raw = await response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProbeStopped("RESPONSE_TOO_LARGE")
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeStopped("INVALID_JSON_RESPONSE") from exc
    finally:
        del raw
    if not isinstance(parsed, dict):
        raise ProbeStopped("RESPONSE_NOT_AN_OBJECT")
    return parsed


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _normalize_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    normalized = str(value).strip()
    return normalized or None


def _id_summary(rows: list[dict[str, Any]], key: str) -> dict[str, object]:
    raw_values = [row.get(key) for row in rows]
    normalized = [value for item in raw_values if (value := _normalize_id(item)) is not None]
    return {
        "present_count": len(normalized),
        "present_on_all_rows": bool(rows) and len(normalized) == len(rows),
        "unique_count": len(set(normalized)),
        "types": sorted({_json_type(value) for value in raw_values}),
        "values_printed": False,
    }


def _parse_request_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ProbeStopped(f"REQUEST_{field.upper()}_MISSING")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProbeStopped(f"REQUEST_{field.upper()}_INVALID") from exc
    if parsed.isoformat() != value:
        raise ProbeStopped(f"REQUEST_{field.upper()}_NOT_CANONICAL")
    return parsed


def _request_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProbeStopped(f"REQUEST_{field.upper()}_INVALID")
    return value


def _safe_label(value: str | None) -> str:
    if not isinstance(value, str):
        raise ProbeStopped("WINDOW_LABEL_MISSING")
    normalized = " ".join(value.split())
    if not re.fullmatch(
        r"[\w\u3400-\u9fff\uff08\uff09()+\-~/ 至今昨本近天日周月年]{1,32}", normalized
    ):
        raise ProbeStopped("WINDOW_LABEL_UNSAFE")
    return normalized


async def _find_exact_page(browser: Browser, target_url: str, code: str) -> Page:
    pages = [
        page
        for context in browser.contexts
        for page in context.pages
        if not page.is_closed() and same_page_url(page.url, target_url)
    ]
    if len(pages) != 1:
        raise ProbeStopped(code)
    return pages[0]


async def _any_visible(page: Page, selector: str) -> bool:
    locator = page.locator(selector)
    return await locator.count() > 0 and await locator.first.is_visible()


async def _assert_safe_page(page: Page, expected_url: str) -> None:
    if page.is_closed() or not same_page_url(page.url, expected_url):
        raise ProbeStopped("AUTH_OR_NAVIGATION_REDIRECT")
    for selector in AUTH_SELECTORS:
        if await _any_visible(page, selector):
            raise ProbeStopped("AUTH_OR_CAPTCHA_PRESENT")
    for phrase in STOP_PHRASES:
        locator = page.get_by_text(phrase, exact=False)
        if await locator.count() > 0 and await locator.first.is_visible():
            raise ProbeStopped("AUTH_CAPTCHA_OR_RISK_NOTICE")


async def _verify_identity(page: Page, expected_sha256: str) -> None:
    await _assert_safe_page(page, IDENTITY_PAGE_URL)
    try:
        async with page.expect_response(
            IDENTITY_RESPONSE_MATCHER, timeout=RESPONSE_TIMEOUT_MS
        ) as response_info:
            await page.reload(wait_until="domcontentloaded", timeout=RESPONSE_TIMEOUT_MS)
        response = await response_info.value
    except Error as exc:
        raise ProbeStopped("IDENTITY_RESPONSE_TIMEOUT") from exc
    await _assert_safe_page(page, IDENTITY_PAGE_URL)
    payload = await _read_json_object(response)
    result = payload.get("result")
    raw_id = result.get("mallId") if isinstance(result, dict) else None
    if (
        payload.get("success") is not True
        or isinstance(raw_id, bool)
        or not isinstance(raw_id, str | int)
    ):
        raise ProbeStopped("IDENTITY_RESPONSE_UNVERIFIED")
    identity = str(raw_id).strip()
    if not identity.isdigit():
        raise ProbeStopped("IDENTITY_RESPONSE_UNVERIFIED")
    try:
        verify_store_identity(identity, "", expected_sha256)
    except CollectionRejected as exc:
        raise ProbeStopped("IDENTITY_MISMATCH") from exc
    page_text = await page.locator("html").text_content()
    if not isinstance(page_text, str) or identity not in page_text:
        raise ProbeStopped("IDENTITY_PAGE_STATE_MISMATCH")


async def _quick_option(page: Page, option_id: str) -> tuple[Locator, str]:
    if option_id not in QUICK_OPTIONS:
        raise ProbeStopped("UNAPPROVED_CLICK_TARGET")
    locator = page.locator(f'[data-testid="{option_id}"]')
    if await locator.count() != 1:
        raise ProbeStopped("WINDOW_OPTION_NOT_UNIQUE")
    if not await locator.is_visible() or not await locator.is_enabled():
        raise ProbeStopped("WINDOW_OPTION_NOT_INTERACTIVE")
    return locator, _safe_label(await locator.text_content())


async def _throttle(last_action_at: float | None) -> None:
    if last_action_at is None:
        return
    elapsed = asyncio.get_running_loop().time() - last_action_at
    remaining = MIN_ACTION_INTERVAL_SECONDS - elapsed
    if remaining > 0:
        await asyncio.sleep(remaining)


async def _click_and_capture(page: Page, locator: Locator) -> tuple[Response, float]:
    try:
        async with page.expect_response(
            PROMOTION_RESPONSE_MATCHER, timeout=RESPONSE_TIMEOUT_MS
        ) as response_info:
            await locator.click(timeout=RESPONSE_TIMEOUT_MS)
        response = await response_info.value
    except Error as exc:
        raise ProbeStopped("PROMOTION_RESPONSE_TIMEOUT") from exc
    return response, asyncio.get_running_loop().time()


async def _reload_and_capture(page: Page) -> tuple[Response, float]:
    try:
        async with page.expect_response(
            PROMOTION_RESPONSE_MATCHER, timeout=RESPONSE_TIMEOUT_MS
        ) as response_info:
            await page.reload(wait_until="domcontentloaded", timeout=RESPONSE_TIMEOUT_MS)
        response = await response_info.value
    except Error as exc:
        raise ProbeStopped("PROMOTION_RESPONSE_TIMEOUT") from exc
    return response, asyncio.get_running_loop().time()


def _row_identity_summary(rows: list[dict[str, Any]], expected_sha256: str) -> dict[str, object]:
    values = [row.get("mallId") for row in rows]
    normalized = [value for item in values if (value := _normalize_id(item)) is not None]
    matches: bool | None = None
    if normalized:
        try:
            for value in normalized:
                verify_store_identity(value, "", expected_sha256)
        except CollectionRejected:
            matches = False
        else:
            matches = len(normalized) == len(rows)
    return {
        "present_count": len(normalized),
        "present_on_all_rows": bool(rows) and len(normalized) == len(rows),
        "unique_count": len(set(normalized)),
        "all_rows_match_expected": matches,
        "values_printed": False,
    }


async def _window_summary(
    response: Response,
    *,
    option_index: int,
    label: str,
    expected_sha256: str,
) -> dict[str, object]:
    request = response.request.post_data_json
    if not isinstance(request, dict):
        raise ProbeStopped("PROMOTION_REQUEST_NOT_AN_OBJECT")
    begin_date = _parse_request_date(request.get("beginDate"), "beginDate")
    end_date = _parse_request_date(request.get("endDate"), "endDate")
    if end_date < begin_date:
        raise ProbeStopped("PROMOTION_REQUEST_DATE_ORDER_INVALID")

    payload = await _read_json_object(response)
    if payload.get("success") is not True:
        raise ProbeStopped("PROMOTION_BUSINESS_FAILURE")
    result = payload.get("result")
    rows_value = result.get("adInfos") if isinstance(result, dict) else None
    if not isinstance(rows_value, list) or not all(isinstance(row, dict) for row in rows_value):
        raise ProbeStopped("PROMOTION_ROWS_UNVERIFIED")
    rows: list[dict[str, Any]] = rows_value
    report_last_update = result.get("reportLastUpdateTime")
    row_identity = _row_identity_summary(rows, expected_sha256)
    if rows and not row_identity["present_on_all_rows"]:
        raise ProbeStopped("PROMOTION_ROW_IDENTITY_MISSING")
    if rows and row_identity["all_rows_match_expected"] is not True:
        raise ProbeStopped("PROMOTION_ROW_IDENTITY_MISMATCH")

    return {
        "option_index": option_index,
        "label": label,
        "request": {
            "beginDate": begin_date.isoformat(),
            "endDate": end_date.isoformat(),
            "inclusive_days": (end_date - begin_date).days + 1,
            "pageNumber": _request_integer(request.get("pageNumber"), "pageNumber"),
            "pageSize": _request_integer(request.get("pageSize"), "pageSize"),
        },
        "success": True,
        "row_count": len(rows),
        "ids": {
            "store_id": row_identity,
            "ad_id": _id_summary(rows, "adId"),
            "plan_id": _id_summary(rows, "planId"),
            "goods_id": _id_summary(rows, "goodsId"),
        },
        "source_update_timestamp": {
            "present": report_last_update is not None,
            "type": _json_type(report_last_update),
            "value_printed": False,
        },
    }


async def probe(expected_sha256: str, max_option_index: int) -> dict[str, object]:
    manager = await async_playwright().start()
    promotion_page: Page | None = None
    restore_required = False
    restore_status = "NOT_REQUIRED"
    last_action_at: float | None = None
    summaries: list[dict[str, object]] = []
    identity_verified = False
    failure: ProbeStopped | None = None
    try:
        try:
            browser = await manager.chromium.connect_over_cdp(
                CDP_ENDPOINT,
                timeout=RESPONSE_TIMEOUT_MS,
                is_local=True,
                no_defaults=True,
            )
            promotion_page = await _find_exact_page(
                browser, PROMOTION_PAGE_URL, "PROMOTION_PAGE_NOT_UNIQUE"
            )
            identity_page = await _find_exact_page(
                browser, IDENTITY_PAGE_URL, "IDENTITY_PAGE_NOT_UNIQUE"
            )
            if promotion_page.context is not identity_page.context:
                raise ProbeStopped("PAGES_NOT_IN_SAME_CONTEXT")
            await _verify_identity(identity_page, expected_sha256)
            identity_verified = True
            await _assert_safe_page(promotion_page, PROMOTION_PAGE_URL)

            options: list[tuple[Locator, str]] = []
            for class_name in QUICK_OPTIONS:
                options.append(await _quick_option(promotion_page, class_name))
            for option_index, (locator, label) in enumerate(options[: max_option_index + 1]):
                await _throttle(last_action_at)
                await _assert_safe_page(promotion_page, PROMOTION_PAGE_URL)
                if option_index == 0:
                    response, last_action_at = await _reload_and_capture(promotion_page)
                else:
                    # The selection may change even if the matching response later times out.
                    restore_required = True
                    response, last_action_at = await _click_and_capture(promotion_page, locator)
                await _assert_safe_page(promotion_page, PROMOTION_PAGE_URL)
                summaries.append(
                    await _window_summary(
                        response,
                        option_index=option_index,
                        label=label,
                        expected_sha256=expected_sha256,
                    )
                )
        except ProbeStopped as exc:
            failure = exc
        except Error:
            failure = ProbeStopped("PLAYWRIGHT_OPERATION_FAILED")
        except Exception:
            failure = ProbeStopped("UNEXPECTED_PROBE_FAILURE")
        finally:
            if promotion_page is not None and restore_required:
                try:
                    # Check before waiting so an auth/captcha/risk state stops immediately.
                    await _assert_safe_page(promotion_page, PROMOTION_PAGE_URL)
                    await _throttle(last_action_at)
                    await _assert_safe_page(promotion_page, PROMOTION_PAGE_URL)
                    _, today_label = await _quick_option(promotion_page, TODAY_OPTION)
                    response, last_action_at = await _reload_and_capture(promotion_page)
                    restored = await _window_summary(
                        response,
                        option_index=0,
                        label=today_label,
                        expected_sha256=expected_sha256,
                    )
                    request = restored["request"]
                    if not isinstance(request, dict) or request.get("inclusive_days") != 1:
                        raise ProbeStopped("RESTORE_TODAY_WINDOW_MISMATCH")
                    await _assert_safe_page(promotion_page, PROMOTION_PAGE_URL)
                    restore_status = "SUCCEEDED"
                except ProbeStopped as exc:
                    restore_status = (
                        "SKIPPED_UNSAFE"
                        if exc.code
                        in {
                            "AUTH_OR_NAVIGATION_REDIRECT",
                            "AUTH_OR_CAPTCHA_PRESENT",
                            "AUTH_CAPTCHA_OR_RISK_NOTICE",
                        }
                        else "FAILED"
                    )
                    if failure is None:
                        failure = ProbeStopped("RESTORE_TODAY_FAILED")
                except Exception:
                    restore_status = "FAILED"
                    if failure is None:
                        failure = ProbeStopped("RESTORE_TODAY_FAILED")
    finally:
        # Disconnect only this Playwright client. Never call browser.close().
        await manager.stop()

    common: dict[str, object] = {
        "identity_verified": identity_verified,
        "identity_value_printed": False,
        "response_bodies_persisted": False,
        "ids_names_or_business_values_printed": False,
        "action_interval_seconds": int(MIN_ACTION_INTERVAL_SECONDS),
        "restore_today": restore_status,
        "windows": summaries,
    }
    if failure is not None:
        return {"status": "STOPPED", "error_code": failure.code, **common}
    return {"status": "PASS", **common}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only probe for bounded promotion date quick options."
    )
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--max-option-index", type=int, choices=range(0, 5), default=4)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256):
        parser.error("--expected-sha256 must be a lowercase SHA-256 digest")
    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required")
    result = asyncio.run(probe(args.expected_sha256, args.max_option_index))
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
