from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error, Page, async_playwright

from pdd_data_mcp.config import load_config

TARGET_PAGE_URL = "https://mms.pinduoduo.com/sycm/goods_effect"
IDENTITY_PAGE_URL = "https://mms.pinduoduo.com/mallcenter/info/basic"
REQUIRED_SAME_CONTEXT_PAGES = (
    "https://yingxiao.pinduoduo.com/mains/promotionOverview",
    "https://mms.pinduoduo.com/goods/goods_list",
    "https://mms.pinduoduo.com/home",
)

MAX_ELEMENT_SCAN = 50_000
MAX_TEXT_NODE_SCAN = 100_000
MAX_CANDIDATE_ELEMENTS = 2_000
MAX_COMMON_ANCESTOR_DEPTH = 6
PLAYWRIGHT_START_TIMEOUT_SECONDS = 10.0
PLAYWRIGHT_STOP_TIMEOUT_SECONDS = 5.0

METRIC_LABELS = {
    "paid_buyer_count": ("支付买家数",),
    "paid_order_count": ("支付订单数",),
    "paid_goods_quantity": ("支付件数", "支付商品件数"),
    "paid_amount": ("支付金额", "支付金额(元)"),
    "visitor_count": ("商品访客数",),
    "page_view_count": ("商品浏览量",),
}

ACCESSIBILITY_SOURCES = frozenset({"aria_label", "aria_valuetext", "input_value"})
NUMERIC_GRAMMAR_CATEGORIES = frozenset(
    {
        "empty",
        "integer",
        "decimal",
        "grouped_integer",
        "grouped_decimal",
        "percent",
        "currency",
        "count_unit",
        "range",
        "placeholder",
        "private_use",
        "other",
    }
)
COMMON_CONTAINER_COUNT_KEYS = frozenset(
    {
        "label_element_count",
        "private_use_container_count",
        "aria_label_numeric_container_count",
        "aria_valuetext_numeric_container_count",
        "input_value_numeric_container_count",
        "any_standardized_numeric_container_count",
    }
)

_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ID_CANDIDATE = re.compile(r"(?<!\d)(?:\d[\s\-_.:]*){5,20}(?!\d)")

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
    'iframe[src*="verify"]',
    'iframe[src*="risk"]',
)

_GATE_SELECTORS = (
    ("LOGIN_REQUIRED", _LOGIN_SELECTORS),
    ("CAPTCHA_PRESENT", _CAPTCHA_SELECTORS),
    ("RISK_CONTROL_PRESENT", _RISK_SELECTORS),
)

_DOM_RESULT_KEYS = frozenset(
    {
        "scanned_element_count",
        "visible_element_count",
        "element_scan_truncated",
        "scanned_text_node_count",
        "text_node_scan_truncated",
        "candidate_scan_truncated",
        "private_use_element_count",
        "private_use_text_node_count",
        "standardized_accessibility_attr_present_counts",
        "numeric_grammar_category_counts",
        "metric_label_common_container_counts",
    }
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
    "EXPECTED_IDENTITY_NOT_CONFIGURED",
    "IDENTITY_PAGE_CONFIG_MISMATCH",
    "PLAYWRIGHT_START_FAILED",
    "CDP_CONNECTION_FAILED",
    "TARGET_PAGE_NOT_FOUND",
    "TARGET_PAGE_AMBIGUOUS",
    "IDENTITY_PAGE_NOT_FOUND",
    "IDENTITY_PAGE_AMBIGUOUS",
    "IDENTITY_PAGE_CONTEXT_MISMATCH",
    "REQUIRED_CONTEXT_PAGE_MISSING",
    "REQUIRED_CONTEXT_PAGE_AMBIGUOUS",
    "LOGIN_REQUIRED",
    "CAPTCHA_PRESENT",
    "RISK_CONTROL_PRESENT",
    "SAFETY_GATE_FAILED",
    "IDENTITY_STATE_UNAVAILABLE",
    "IDENTITY_UNVERIFIED",
    "IDENTITY_MISMATCH",
    "ACCESSIBILITY_PROBE_FAILED",
    "ACCESSIBILITY_RESULT_INVALID",
    "PLAYWRIGHT_STOP_FAILED",
    "UNEXPECTED_FAILURE",
]


class AccessibilityStopped(RuntimeError):
    def __init__(self, code: ErrorCode, counts: SafetyCounts | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.counts = counts


@dataclass(frozen=True)
class AccessibilityRuntime:
    cdp_endpoint: str
    connect_timeout_ms: int
    collection_timeout_ms: int
    expected_store_id: str
    expected_store_id_sha256: str


@dataclass
class SafetyCounts:
    reload_actions: int = 0
    navigation_actions: int = 0
    click_actions: int = 0
    scroll_actions: int = 0
    page_close_actions: int = 0
    browser_close_actions: int = 0
    response_listener_registrations: int = 0
    response_body_reads: int = 0
    request_body_reads: int = 0
    filesystem_writes: int = 0
    snapshot_writes: int = 0
    capability_enable_actions: int = 0
    font_inspections: int = 0
    canvas_reads: int = 0
    image_or_ocr_reads: int = 0
    arbitrary_data_attribute_reads: int = 0
    script_state_reads: int = 0
    hidden_response_reads: int = 0
    raw_text_outputs: int = 0
    accessibility_value_outputs: int = 0
    metric_value_outputs: int = 0
    identity_value_outputs: int = 0
    product_id_outputs: int = 0
    product_name_outputs: int = 0
    class_outputs: int = 0
    title_outputs: int = 0
    query_outputs: int = 0
    full_url_outputs: int = 0
    safety_selector_checks: int = 0
    identity_dom_reads: int = 0
    accessibility_dom_reads: int = 0


_ACCESSIBILITY_PROBE_SCRIPT = r"""
payload => {
    const maxElements = payload.max_elements;
    const maxTextNodes = payload.max_text_nodes;
    const maxCandidates = payload.max_candidates;
    const maxAncestorDepth = payload.max_ancestor_depth;
    const metricLabels = payload.metric_labels;

    const emptyCategoryCounts = () => ({
        empty: 0,
        integer: 0,
        decimal: 0,
        grouped_integer: 0,
        grouped_decimal: 0,
        percent: 0,
        currency: 0,
        count_unit: 0,
        range: 0,
        placeholder: 0,
        private_use: 0,
        other: 0,
    });
    const normalize = value => String(value).normalize('NFKC').trim();
    const hasPrivateUse = value => {
        for (const character of String(value)) {
            const point = character.codePointAt(0);
            if (
                (point >= 0xE000 && point <= 0xF8FF)
                || (point >= 0xF0000 && point <= 0xFFFFD)
                || (point >= 0x100000 && point <= 0x10FFFD)
            ) return true;
        }
        return false;
    };
    const classify = raw => {
        const value = normalize(raw);
        if (!value) return 'empty';
        if (hasPrivateUse(value)) return 'private_use';
        if (/^(?:-|--|\u2014|\u2013|N\/?A|NULL|暂无|无数据)$/iu.test(value)) {
            return 'placeholder';
        }
        const number = '-?(?:0|[1-9]\\d*)';
        const decimal = `${number}\\.\\d+`;
        const groupedInteger = '-?\\d{1,3}(?:,\\d{3})+';
        const groupedDecimal = `${groupedInteger}\\.\\d+`;
        const anyNumber = `(?:${decimal}|${groupedDecimal}|${groupedInteger}|${number})`;
        if (new RegExp(`^${anyNumber}[%\uFF05]$`, 'u').test(value)) return 'percent';
        if (
            new RegExp(`^(?:[¥￥]${anyNumber}|${anyNumber}元)$`, 'u').test(value)
        ) return 'currency';
        if (new RegExp(`^${anyNumber}(?:人|单|件|次)$`, 'u').test(value)) {
            return 'count_unit';
        }
        if (
            new RegExp(
                `^${anyNumber}\\s*(?:~|\uFF5E|至|\u2013|\u2014)\\s*${anyNumber}`
                    + '(?:人|单|件|次|元|[%\uFF05])?$',
                'u',
            ).test(value)
        ) return 'range';
        if (new RegExp(`^${groupedDecimal}$`, 'u').test(value)) return 'grouped_decimal';
        if (new RegExp(`^${groupedInteger}$`, 'u').test(value)) return 'grouped_integer';
        if (new RegExp(`^${decimal}$`, 'u').test(value)) return 'decimal';
        if (new RegExp(`^${number}$`, 'u').test(value)) return 'integer';
        return 'other';
    };
    const visible = element => {
        const style = getComputedStyle(element);
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && style.visibility !== 'collapse'
            && Number(style.opacity) !== 0
            && element.getClientRects().length > 0;
    };
    const visibleTextNode = node => {
        const parent = node.parentElement;
        if (parent === null || !visible(parent)) return false;
        const range = document.createRange();
        range.selectNodeContents(node);
        const shown = range.getClientRects().length > 0;
        range.detach();
        return shown;
    };
    const numericCategory = category => ![
        'empty', 'placeholder', 'private_use', 'other'
    ].includes(category);
    const nearestContainer = (label, requiredElements) => {
        let container = label;
        for (let depth = 0; depth <= maxAncestorDepth && container !== null; depth += 1) {
            if (
                container !== document.body
                && container !== document.documentElement
                && visible(container)
                && requiredElements.every(
                    candidates => candidates.some(candidate => container.contains(candidate))
                )
            ) return container;
            container = container.parentElement;
        }
        return null;
    };

    const allElements = Array.from(document.querySelectorAll('body *'));
    const elements = allElements.slice(0, maxElements);
    const visibleElements = elements.filter(visible);
    const privateUseElements = [];
    const privateUseElementSet = new Set();
    const labelElements = {};
    for (const code of Object.keys(metricLabels)) labelElements[code] = new Set();
    let scannedTextNodeCount = 0;
    let privateUseTextNodeCount = 0;
    let textNodeScanTruncated = false;

    outer: for (const element of visibleElements) {
        for (const node of element.childNodes) {
            if (node.nodeType !== Node.TEXT_NODE) continue;
            if (scannedTextNodeCount >= maxTextNodes) {
                textNodeScanTruncated = true;
                break outer;
            }
            scannedTextNodeCount += 1;
            if (!visibleTextNode(node)) continue;
            const raw = typeof node.data === 'string' ? node.data : '';
            if (hasPrivateUse(raw)) {
                privateUseTextNodeCount += 1;
                if (!privateUseElementSet.has(element)) {
                    privateUseElementSet.add(element);
                    if (privateUseElements.length < maxCandidates) {
                        privateUseElements.push(element);
                    }
                }
            }
            const normalized = normalize(raw).replace(/[\s\u200b\ufeff]+/gu, '');
            for (const [code, labels] of Object.entries(metricLabels)) {
                if (labels.some(label => normalized === normalize(label))) {
                    labelElements[code].add(element);
                }
            }
        }
    }

    const attrCounts = {aria_label: 0, aria_valuetext: 0, input_value: 0, any: 0};
    const categoryCounts = {
        aria_label: emptyCategoryCounts(),
        aria_valuetext: emptyCategoryCounts(),
        input_value: emptyCategoryCounts(),
    };
    const numericCarriers = {aria_label: [], aria_valuetext: [], input_value: []};
    let candidateScanTruncated = privateUseElementSet.size > privateUseElements.length;

    for (const element of visibleElements) {
        let anyPresent = false;
        if (element.hasAttribute('aria-label')) {
            anyPresent = true;
            attrCounts.aria_label += 1;
            const category = classify(element.getAttribute('aria-label'));
            categoryCounts.aria_label[category] += 1;
            if (numericCategory(category)) {
                if (numericCarriers.aria_label.length < maxCandidates) {
                    numericCarriers.aria_label.push(element);
                } else candidateScanTruncated = true;
            }
        }
        if (element.hasAttribute('aria-valuetext')) {
            anyPresent = true;
            attrCounts.aria_valuetext += 1;
            const category = classify(element.getAttribute('aria-valuetext'));
            categoryCounts.aria_valuetext[category] += 1;
            if (numericCategory(category)) {
                if (numericCarriers.aria_valuetext.length < maxCandidates) {
                    numericCarriers.aria_valuetext.push(element);
                } else candidateScanTruncated = true;
            }
        }
        if (element instanceof HTMLInputElement && element.type.toLowerCase() !== 'password') {
            anyPresent = true;
            attrCounts.input_value += 1;
            const category = classify(element.value);
            categoryCounts.input_value[category] += 1;
            if (numericCategory(category)) {
                if (numericCarriers.input_value.length < maxCandidates) {
                    numericCarriers.input_value.push(element);
                } else candidateScanTruncated = true;
            }
        }
        if (anyPresent) attrCounts.any += 1;
    }

    const commonCounts = {};
    for (const [code, labels] of Object.entries(labelElements)) {
        const privateContainers = new Set();
        const ariaLabelContainers = new Set();
        const ariaValueTextContainers = new Set();
        const inputValueContainers = new Set();
        const anyNumericContainers = new Set();
        const allNumericCarriers = [
            ...numericCarriers.aria_label,
            ...numericCarriers.aria_valuetext,
            ...numericCarriers.input_value,
        ];
        for (const label of labels) {
            const privateContainer = nearestContainer(label, [privateUseElements]);
            if (privateContainer !== null) privateContainers.add(privateContainer);
            const ariaLabel = nearestContainer(
                label, [privateUseElements, numericCarriers.aria_label]
            );
            if (ariaLabel !== null) ariaLabelContainers.add(ariaLabel);
            const ariaValueText = nearestContainer(
                label, [privateUseElements, numericCarriers.aria_valuetext]
            );
            if (ariaValueText !== null) ariaValueTextContainers.add(ariaValueText);
            const inputValue = nearestContainer(
                label, [privateUseElements, numericCarriers.input_value]
            );
            if (inputValue !== null) inputValueContainers.add(inputValue);
            const anyNumeric = nearestContainer(label, [privateUseElements, allNumericCarriers]);
            if (anyNumeric !== null) anyNumericContainers.add(anyNumeric);
        }
        commonCounts[code] = {
            label_element_count: labels.size,
            private_use_container_count: privateContainers.size,
            aria_label_numeric_container_count: ariaLabelContainers.size,
            aria_valuetext_numeric_container_count: ariaValueTextContainers.size,
            input_value_numeric_container_count: inputValueContainers.size,
            any_standardized_numeric_container_count: anyNumericContainers.size,
        };
    }

    return {
        scanned_element_count: elements.length,
        visible_element_count: visibleElements.length,
        element_scan_truncated: allElements.length > elements.length,
        scanned_text_node_count: scannedTextNodeCount,
        text_node_scan_truncated: textNodeScanTruncated,
        candidate_scan_truncated: candidateScanTruncated,
        private_use_element_count: privateUseElementSet.size,
        private_use_text_node_count: privateUseTextNodeCount,
        standardized_accessibility_attr_present_counts: attrCounts,
        numeric_grammar_category_counts: categoryCounts,
        metric_label_common_container_counts: commonCounts,
    };
}
"""


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


def _load_runtime(config_path: Path, connection_id: str) -> AccessibilityRuntime:
    if _CONNECTION_ID.fullmatch(connection_id) is None:
        raise AccessibilityStopped("INVALID_CONNECTION_ID")
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise AccessibilityStopped("CONFIG_LOAD_FAILED") from exc
    try:
        connection = config.connection(connection_id)
    except KeyError as exc:
        raise AccessibilityStopped("CONNECTION_ID_NOT_FOUND") from exc
    if config.service.test_mode:
        raise AccessibilityStopped("REAL_MODE_REQUIRED")
    if not connection.real_collection_enabled:
        raise AccessibilityStopped("REAL_COLLECTION_NOT_ENABLED")
    if connection.cdp_endpoint is None:
        raise AccessibilityStopped("CDP_ENDPOINT_NOT_CONFIGURED")
    if not _is_trusted_loopback_endpoint(connection.cdp_endpoint):
        raise AccessibilityStopped("CDP_ENDPOINT_NOT_TRUSTED")
    if bool(connection.expected_platform_store_id) == bool(
        connection.expected_platform_store_id_sha256
    ):
        raise AccessibilityStopped("EXPECTED_IDENTITY_NOT_CONFIGURED")
    identity_page_url = connection.product_catalog_adapter.identity_page_url
    if not _matches_exact_page(identity_page_url, IDENTITY_PAGE_URL):
        raise AccessibilityStopped("IDENTITY_PAGE_CONFIG_MISMATCH")
    return AccessibilityRuntime(
        cdp_endpoint=connection.cdp_endpoint,
        connect_timeout_ms=config.collection.connect_timeout_ms,
        collection_timeout_ms=config.collection.collection_timeout_ms,
        expected_store_id=connection.expected_platform_store_id,
        expected_store_id_sha256=connection.expected_platform_store_id_sha256,
    )


def _select_pages(browser: Browser) -> tuple[Page, Page, tuple[Page, ...], int, int]:
    contexts = list(browser.contexts)
    open_pages = [page for context in contexts for page in context.pages if not page.is_closed()]
    targets = [page for page in open_pages if _matches_exact_page(page.url, TARGET_PAGE_URL)]
    if not targets:
        raise AccessibilityStopped("TARGET_PAGE_NOT_FOUND")
    if len(targets) != 1:
        raise AccessibilityStopped("TARGET_PAGE_AMBIGUOUS")
    target = targets[0]
    identity_pages = [
        page for page in open_pages if _matches_exact_page(page.url, IDENTITY_PAGE_URL)
    ]
    if not identity_pages:
        raise AccessibilityStopped("IDENTITY_PAGE_NOT_FOUND")
    if len(identity_pages) != 1:
        raise AccessibilityStopped("IDENTITY_PAGE_AMBIGUOUS")
    identity_page = identity_pages[0]
    if identity_page.context is not target.context:
        raise AccessibilityStopped("IDENTITY_PAGE_CONTEXT_MISMATCH")
    required_pages: list[Page] = []
    for required_url in REQUIRED_SAME_CONTEXT_PAGES:
        matches = [
            page
            for page in target.context.pages
            if not page.is_closed() and _matches_exact_page(page.url, required_url)
        ]
        if not matches:
            raise AccessibilityStopped("REQUIRED_CONTEXT_PAGE_MISSING")
        if len(matches) != 1:
            raise AccessibilityStopped("REQUIRED_CONTEXT_PAGE_AMBIGUOUS")
        required_pages.append(matches[0])
    return target, identity_page, tuple(required_pages), len(contexts), len(open_pages)


async def _assert_safety_gate(page: Page, counts: SafetyCounts, *, timeout_seconds: float) -> None:
    for raw_code, selectors in _GATE_SELECTORS:
        code = cast(ErrorCode, raw_code)
        for selector in selectors:
            counts.safety_selector_checks += 1
            try:
                present = await asyncio.wait_for(
                    page.locator(selector).count(), timeout=timeout_seconds
                )
            except (Error, TimeoutError) as exc:
                raise AccessibilityStopped("SAFETY_GATE_FAILED") from exc
            if present > 0:
                raise AccessibilityStopped(code)


async def _verify_identity(
    page: Page,
    runtime: AccessibilityRuntime,
    counts: SafetyCounts,
    *,
    timeout_seconds: float,
) -> None:
    counts.identity_dom_reads += 1
    try:
        text = await asyncio.wait_for(
            # Keep this probe on rendered identity text. The broader repository
            # identity helper reads all HTML text, which can include script state
            # and is intentionally outside this probe's narrower safety contract.
            page.locator("body").inner_text(timeout=runtime.collection_timeout_ms),
            timeout=timeout_seconds,
        )
    except (Error, TimeoutError) as exc:
        raise AccessibilityStopped("IDENTITY_STATE_UNAVAILABLE") from exc
    if not text:
        raise AccessibilityStopped("IDENTITY_STATE_UNAVAILABLE")
    matches: set[str] = set()
    candidate_seen = False
    for raw in _ID_CANDIDATE.findall(text):
        candidate = "".join(character for character in raw if character.isdigit())
        if not 5 <= len(candidate) <= 20:
            continue
        candidate_seen = True
        if runtime.expected_store_id:
            if candidate == runtime.expected_store_id:
                matches.add("matched-configured-identity")
        elif (
            hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            == runtime.expected_store_id_sha256
        ):
            matches.add("matched-configured-identity")
    if len(matches) != 1:
        code: ErrorCode = "IDENTITY_MISMATCH" if candidate_seen else "IDENTITY_UNVERIFIED"
        raise AccessibilityStopped(code)


def _strict_count(value: object, *, maximum: int = MAX_TEXT_NODE_SCAN) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    return value


def _strict_count_map(
    raw: object, *, expected_keys: frozenset[str], maximum: int
) -> dict[str, int]:
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    return {key: _strict_count(raw.get(key), maximum=maximum) for key in sorted(expected_keys)}


def _validate_dom_result(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != _DOM_RESULT_KEYS:
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    scanned_elements = _strict_count(raw.get("scanned_element_count"), maximum=MAX_ELEMENT_SCAN)
    visible_elements = _strict_count(raw.get("visible_element_count"), maximum=MAX_ELEMENT_SCAN)
    scanned_text_nodes = _strict_count(
        raw.get("scanned_text_node_count"), maximum=MAX_TEXT_NODE_SCAN
    )
    private_use_elements = _strict_count(
        raw.get("private_use_element_count"), maximum=MAX_ELEMENT_SCAN
    )
    private_use_text_nodes = _strict_count(
        raw.get("private_use_text_node_count"), maximum=MAX_TEXT_NODE_SCAN
    )
    if visible_elements > scanned_elements or private_use_elements > visible_elements:
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    if private_use_text_nodes > scanned_text_nodes:
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    for key in (
        "element_scan_truncated",
        "text_node_scan_truncated",
        "candidate_scan_truncated",
    ):
        if type(raw.get(key)) is not bool:
            raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")

    attr_keys = frozenset((*ACCESSIBILITY_SOURCES, "any"))
    attr_counts = _strict_count_map(
        raw.get("standardized_accessibility_attr_present_counts"),
        expected_keys=attr_keys,
        maximum=visible_elements,
    )
    if attr_counts["any"] < max(attr_counts[source] for source in ACCESSIBILITY_SOURCES):
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")

    raw_categories = raw.get("numeric_grammar_category_counts")
    if not isinstance(raw_categories, dict) or set(raw_categories) != ACCESSIBILITY_SOURCES:
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    categories: dict[str, dict[str, int]] = {}
    for source in sorted(ACCESSIBILITY_SOURCES):
        source_counts = _strict_count_map(
            raw_categories.get(source),
            expected_keys=NUMERIC_GRAMMAR_CATEGORIES,
            maximum=visible_elements,
        )
        if sum(source_counts.values()) != attr_counts[source]:
            raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
        categories[source] = source_counts

    raw_common = raw.get("metric_label_common_container_counts")
    if not isinstance(raw_common, dict) or set(raw_common) != set(METRIC_LABELS):
        raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
    common: dict[str, dict[str, int]] = {}
    for metric_code in sorted(METRIC_LABELS):
        metric_counts = _strict_count_map(
            raw_common.get(metric_code),
            expected_keys=COMMON_CONTAINER_COUNT_KEYS,
            maximum=visible_elements,
        )
        label_count = metric_counts["label_element_count"]
        if any(
            metric_counts[key] > label_count
            for key in COMMON_CONTAINER_COUNT_KEYS
            if key != "label_element_count"
        ):
            raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
        if any(
            metric_counts[key] > metric_counts["private_use_container_count"]
            for key in (
                "aria_label_numeric_container_count",
                "aria_valuetext_numeric_container_count",
                "input_value_numeric_container_count",
                "any_standardized_numeric_container_count",
            )
        ):
            raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
        if metric_counts["any_standardized_numeric_container_count"] < max(
            metric_counts["aria_label_numeric_container_count"],
            metric_counts["aria_valuetext_numeric_container_count"],
            metric_counts["input_value_numeric_container_count"],
        ):
            raise AccessibilityStopped("ACCESSIBILITY_RESULT_INVALID")
        common[metric_code] = metric_counts

    return {
        "scan_counts": {
            "scanned_element_count": scanned_elements,
            "visible_element_count": visible_elements,
            "scanned_text_node_count": scanned_text_nodes,
            "element_scan_truncated": raw["element_scan_truncated"],
            "text_node_scan_truncated": raw["text_node_scan_truncated"],
            "candidate_scan_truncated": raw["candidate_scan_truncated"],
        },
        "private_use_counts": {
            "element_count": private_use_elements,
            "text_node_count": private_use_text_nodes,
        },
        "standardized_accessibility_attr_present_counts": attr_counts,
        "numeric_grammar_category_counts": categories,
        "metric_label_common_container_counts": common,
    }


def _safety_payload(counts: SafetyCounts) -> dict[str, int]:
    return {field: cast(int, getattr(counts, field)) for field in counts.__dataclass_fields__}


async def inspect_browser(
    browser: Browser, runtime: AccessibilityRuntime, counts: SafetyCounts
) -> dict[str, object]:
    target, identity_page, required_pages, context_count, open_page_count = _select_pages(browser)
    timeout_seconds = max(0.001, runtime.collection_timeout_ms / 1_000)
    for page in (target, identity_page, *required_pages):
        await _assert_safety_gate(page, counts, timeout_seconds=timeout_seconds)
    await _verify_identity(
        identity_page,
        runtime,
        counts,
        timeout_seconds=timeout_seconds,
    )
    await _assert_safety_gate(target, counts, timeout_seconds=timeout_seconds)
    counts.accessibility_dom_reads += 1
    try:
        raw: Any = await asyncio.wait_for(
            target.evaluate(
                _ACCESSIBILITY_PROBE_SCRIPT,
                {
                    "max_elements": MAX_ELEMENT_SCAN,
                    "max_text_nodes": MAX_TEXT_NODE_SCAN,
                    "max_candidates": MAX_CANDIDATE_ELEMENTS,
                    "max_ancestor_depth": MAX_COMMON_ANCESTOR_DEPTH,
                    "metric_labels": METRIC_LABELS,
                },
            ),
            timeout=timeout_seconds,
        )
    except (Error, TimeoutError) as exc:
        raise AccessibilityStopped("ACCESSIBILITY_PROBE_FAILED") from exc
    observations = _validate_dom_result(raw)
    return {
        "status": "PASS",
        "error_code": None,
        "identity_verified": True,
        "page_gate_counts": {
            "context_count": context_count,
            "open_page_count": open_page_count,
            "target_page_count": 1,
            "identity_page_count": 1,
            "required_same_context_page_count": len(required_pages),
        },
        **observations,
        "safety_counts": _safety_payload(counts),
    }


async def probe(runtime: AccessibilityRuntime) -> dict[str, object]:
    counts = SafetyCounts()
    try:
        manager = await asyncio.wait_for(
            async_playwright().start(), timeout=PLAYWRIGHT_START_TIMEOUT_SECONDS
        )
    except (Error, TimeoutError) as exc:
        raise AccessibilityStopped("PLAYWRIGHT_START_FAILED") from exc

    stopped: AccessibilityStopped | None = None
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
            raise AccessibilityStopped("CDP_CONNECTION_FAILED") from exc
        result = await inspect_browser(browser, runtime, counts)
    except AccessibilityStopped as exc:
        if exc.counts is None:
            exc.counts = counts
        stopped = exc
    finally:
        try:
            await asyncio.wait_for(manager.stop(), timeout=PLAYWRIGHT_STOP_TIMEOUT_SECONDS)
        except (Error, TimeoutError) as exc:
            raise AccessibilityStopped("PLAYWRIGHT_STOP_FAILED") from exc
    if stopped is not None:
        raise stopped
    assert result is not None
    return result


def _error_payload(code: ErrorCode, counts: SafetyCounts | None = None) -> dict[str, object]:
    return {
        "status": "STOPPED",
        "error_code": code,
        "private_use_counts": None,
        "standardized_accessibility_attr_present_counts": None,
        "numeric_grammar_category_counts": None,
        "metric_label_common_container_counts": None,
        "safety_counts": _safety_payload(counts or SafetyCounts()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Count redacted accessibility fallback categories on one already-open product page."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    try:
        if not args.confirm_read_only:
            raise AccessibilityStopped("READ_ONLY_CONFIRMATION_REQUIRED")
        runtime = _load_runtime(args.config, args.connection_id)
        payload = asyncio.run(probe(runtime))
    except AccessibilityStopped as exc:
        payload = _error_payload(exc.code, exc.counts)
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
