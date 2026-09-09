from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Error, Page, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url
from pdd_data_mcp.config import AppConfig, ConnectionSettings, load_config

_TARGET_URL = "https://mms.pinduoduo.com/sycm/goods_effect"
_REQUIRED_CONTEXT_URLS = {
    "promotion_overview_present": "https://yingxiao.pinduoduo.com/mains/promotionOverview",
    "goods_list_present": "https://mms.pinduoduo.com/goods/goods_list",
    "home_present": "https://mms.pinduoduo.com/home",
}
_BUSINESS_LABELS = {
    "product_id": "商品ID",
    "paid_buyer": "支付买家",
    "paid_order": "支付订单",
    "sales_quantity": "销量",
    "paid_amount": "支付金额",
    "visitor": "访客",
    "page_view": "浏览量",
}
_TIME_LABELS = {
    "yesterday": "昨日",
    "last_7_days_tian": "近7天",
    "last_7_days_ri": "近7日",
}
_FIXED_LOGIN_SELECTORS = ('input[type="password"]',)
_FIXED_CAPTCHA_SELECTORS = (
    'input[placeholder*="验证码"]',
    'iframe[src*="captcha"]',
    '[class*="Captcha"]',
    '[class*="captcha"]',
)
_FIXED_RISK_SELECTORS = (
    'iframe[src*="risk"]',
    '[class*="Risk"]',
    '[class*="risk"]',
    '[data-testid*="Risk"]',
    '[data-testid*="risk"]',
)

_COUNT_LABELS_SCRIPT = r"""
groups => {
    const normalize = value => String(value)
        .normalize('NFKC')
        .replace(/[\s\u200b\ufeff]+/gu, '')
        .replace(/[\uFF1A:]+$/u, '');
    const texts = [];
    for (const element of document.querySelectorAll('body *')) {
        const text = typeof element.innerText === 'string' ? element.innerText : '';
        if (text) texts.push(text);
    }
    const result = {};
    for (const [groupName, labels] of Object.entries(groups)) {
        const groupResult = {};
        for (const [code, label] of Object.entries(labels)) {
            let exactCount = 0;
            let normalizedCount = 0;
            let normalizedContainsCount = 0;
            const normalizedLabel = normalize(label);
            for (const text of texts) {
                if (text === label) exactCount += 1;
                const normalizedText = normalize(text);
                if (normalizedText === normalizedLabel) normalizedCount += 1;
                if (normalizedText.includes(normalizedLabel)) normalizedContainsCount += 1;
            }
            groupResult[code] = {
                exact_count: exactCount,
                normalized_count: normalizedCount,
                normalized_contains_count: normalizedContainsCount,
            };
        }
        result[groupName] = groupResult;
    }
    return result;
}
"""


class ProbeStopped(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _safety_selectors(connection: ConnectionSettings) -> dict[str, tuple[str, ...]]:
    adapters = (
        connection.product_catalog_adapter,
        connection.inventory_adapter,
        connection.store_overview_adapter,
    )
    return {
        "login": _unique(
            (*_FIXED_LOGIN_SELECTORS, *(adapter.login_selector for adapter in adapters))
        ),
        "captcha": _unique(
            (*_FIXED_CAPTCHA_SELECTORS, *(adapter.captcha_selector for adapter in adapters))
        ),
        "risk": _unique(
            (*_FIXED_RISK_SELECTORS, *(adapter.error_selector for adapter in adapters))
        ),
    }


async def _selector_group_present(page: Page, selectors: tuple[str, ...]) -> bool:
    try:
        for selector in selectors:
            if await page.locator(selector).count() > 0:
                return True
    except Error as exc:
        raise ProbeStopped("SAFETY_SELECTOR_CHECK_FAILED") from exc
    return False


def _validated_group(raw: object, expected_codes: set[str]) -> dict[str, dict[str, int | bool]]:
    if not isinstance(raw, dict) or set(raw) != expected_codes:
        raise ProbeStopped("DOM_LABEL_COUNT_RESULT_INVALID")
    result: dict[str, dict[str, int | bool]] = {}
    for code in sorted(expected_codes):
        counts = raw.get(code)
        if not isinstance(counts, dict) or set(counts) != {
            "exact_count",
            "normalized_count",
            "normalized_contains_count",
        }:
            raise ProbeStopped("DOM_LABEL_COUNT_RESULT_INVALID")
        exact = counts.get("exact_count")
        normalized = counts.get("normalized_count")
        normalized_contains = counts.get("normalized_contains_count")
        if (
            type(exact) is not int
            or exact < 0
            or type(normalized) is not int
            or normalized < 0
            or type(normalized_contains) is not int
            or normalized_contains < 0
        ):
            raise ProbeStopped("DOM_LABEL_COUNT_RESULT_INVALID")
        result[code] = {
            "exact_present": exact > 0,
            "exact_count": exact,
            "normalized_present": normalized > 0,
            "normalized_count": normalized,
            "normalized_contains_present": normalized_contains > 0,
            "normalized_contains_count": normalized_contains,
        }
    return result


async def inspect_browser(
    browser: Browser, *, safety_selectors: dict[str, tuple[str, ...]]
) -> dict[str, object]:
    pages = [page for context in browser.contexts for page in context.pages if not page.is_closed()]
    matching = [page for page in pages if same_page_url(page.url, _TARGET_URL)]
    if not matching:
        raise ProbeStopped("TARGET_PAGE_NOT_FOUND")
    if len(matching) != 1:
        raise ProbeStopped("AMBIGUOUS_TARGET_PAGE")
    page = matching[0]
    same_context_pages = [
        candidate for candidate in page.context.pages if not candidate.is_closed()
    ]
    context_checks = {
        code: any(same_page_url(candidate.url, required) for candidate in same_context_pages)
        for code, required in _REQUIRED_CONTEXT_URLS.items()
    }
    if not all(context_checks.values()):
        raise ProbeStopped("REQUIRED_SAME_CONTEXT_PAGE_MISSING")

    for group, error_code in (
        ("login", "LOGIN_SELECTOR_PRESENT"),
        ("captcha", "CAPTCHA_SELECTOR_PRESENT"),
        ("risk", "RISK_SELECTOR_PRESENT"),
    ):
        if await _selector_group_present(page, safety_selectors.get(group, ())):
            raise ProbeStopped(error_code)

    try:
        raw_counts: Any = await page.evaluate(
            _COUNT_LABELS_SCRIPT,
            {"business": _BUSINESS_LABELS, "time": _TIME_LABELS},
        )
    except Error as exc:
        raise ProbeStopped("DOM_LABEL_COUNT_FAILED") from exc
    if not isinstance(raw_counts, dict) or set(raw_counts) != {"business", "time"}:
        raise ProbeStopped("DOM_LABEL_COUNT_RESULT_INVALID")

    return {
        "status": "PASS",
        "target_page_unique": True,
        "same_context_requirements": context_checks,
        "safety_selectors_clear": {
            "login": True,
            "captcha": True,
            "risk": True,
        },
        "business_labels": _validated_group(raw_counts["business"], set(_BUSINESS_LABELS)),
        "time_labels": _validated_group(raw_counts["time"], set(_TIME_LABELS)),
        "page_original_text_output": False,
        "page_business_values_output": False,
        "page_identity_or_name_output": False,
        "page_title_query_or_url_output": False,
        "browser_navigation_or_click": False,
    }


async def probe(config: AppConfig, connection_id: str) -> dict[str, object]:
    try:
        connection = config.connection(connection_id)
    except KeyError as exc:
        raise ProbeStopped("UNKNOWN_CONNECTION") from exc
    if not connection.real_collection_enabled:
        raise ProbeStopped("REAL_COLLECTION_DISABLED")
    if connection.cdp_endpoint is None:
        raise ProbeStopped("CDP_ENDPOINT_NOT_CONFIGURED")

    manager = await async_playwright().start()
    try:
        try:
            browser = await manager.chromium.connect_over_cdp(
                connection.cdp_endpoint,
                timeout=config.collection.connect_timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            raise ProbeStopped("CDP_CONNECTION_FAILED") from exc
        return await inspect_browser(browser, safety_selectors=_safety_selectors(connection))
    finally:
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded read-only DOM label presence probe for the open product page."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(load_config(args.config), args.connection_id))
    except ProbeStopped as exc:
        print(
            json.dumps(
                {"status": "STOPPED", "error_code": exc.code, "browser_write_actions": 0},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            json.dumps(
                {
                    "status": "STOPPED",
                    "error_code": "PROBE_FAILED",
                    "browser_write_actions": 0,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
