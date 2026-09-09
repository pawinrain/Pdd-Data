from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Error, Locator, Page, Response, async_playwright

from pdd_data_mcp.browser.promotion import sanitize_discovery_path
from pdd_data_mcp.config import StorageSettings, load_config
from pdd_data_mcp.contracts.models import DatasetType
from pdd_data_mcp.errors import (
    AuthorizationError,
    LockBusyError,
    SnapshotCorruptError,
    StorageError,
    ValidationFailure,
)
from pdd_data_mcp.storage import LocalFileSnapshotRepository

TARGET_PAGE_URL = "https://mms.pinduoduo.com/sycm/goods_effect"
IDENTITY_PAGE_URL = "https://mms.pinduoduo.com/mallcenter/info/basic"
REQUIRED_SAME_CONTEXT_PAGES = (
    "https://yingxiao.pinduoduo.com/mains/promotionOverview",
    "https://mms.pinduoduo.com/goods/goods_list",
    "https://mms.pinduoduo.com/home",
)
RESPONSE_HOST = "mms.pinduoduo.com"
LIST_RESPONSE_PATH = "/sydney/api/goodsDataShow/queryGoodsDetailVOListForMMS"
READY_DATE_RESPONSE_PATH = "/sydney/api/goodsDataShow/queryGoodsReadyDate"
MAX_RESPONSE_BYTES = 1_048_576
MAX_CANDIDATE_OBSERVATIONS = 20
MAX_CRAWLER_INFO_LENGTH = 4_096
OBSERVE_SECONDS = 8.0
MIN_TAB_ACTION_INTERVAL_SECONDS = 60.0
TASK_CANCEL_GRACE_SECONDS = 1.0
PLAYWRIGHT_STOP_TIMEOUT_SECONDS = 5.0
DOM_EVALUATE_TIMEOUT_SECONDS = 5.0
CLI_BASE_DEADLINE_SECONDS = 240.0
CLI_TAB_DEADLINE_SECONDS = 480.0
CLI_TAB_INVENTORY_DEADLINE_SECONDS = 60.0
CLI_PRODUCT_CONTROLS_DEADLINE_SECONDS = 60.0

_CHECKPOINT_CODES = frozenset(
    {
        "BASE_CAPTURE_BEGIN",
        "BASE_CAPTURE_DONE",
        "CATALOG_LOAD_BEGIN",
        "CATALOG_LOAD_DONE",
        "CDP_CONNECT_BEGIN",
        "CDP_CONNECT_DONE",
        "CLI_ARGS_VALIDATED",
        "CLI_RESULT_READY",
        "CLI_RUNTIME_LOADED",
        "CLI_STOPPED",
        "IDENTITY_GATE_DONE",
        "PAGE_GATE_DONE",
        "PLAYWRIGHT_START_BEGIN",
        "PLAYWRIGHT_START_DONE",
        "PLAYWRIGHT_STOP_BEGIN",
        "PLAYWRIGHT_STOP_DONE",
        "PLAYWRIGHT_STOP_TIMEOUT",
        "PRODUCT_CONTROLS_BEGIN",
        "PRODUCT_CONTROLS_DONE",
        "RUNNER_ENTER",
        "RUNNER_PENDING_TASKS_ABANDONED",
        "RUNNER_SHUTDOWN_BEGIN",
        "RUNNER_SHUTDOWN_DONE",
        "TAB_CAPTURE_BEGIN",
        "TAB_CAPTURE_DONE",
        "TOTAL_DEADLINE_EXCEEDED",
    }
)

LIST_REQUEST_KEYS = frozenset(
    {
        "actVs",
        "crawlerInfo",
        "endDate",
        "pageNum",
        "pageSize",
        "queryType",
        "sortCol",
        "sortType",
        "startDate",
    }
)
LIST_RESULT_KEYS = frozenset({"delayData", "goodsDetailList", "totalNum", "timestamp"})
LIST_ROW_KEYS = frozenset(
    {
        "goodsId",
        "goodsName",
        "goodsStatus",
        "statDate",
        "payOrdrUsrCnt",
        "payOrdrCnt",
        "payOrdrGoodsQty",
        "payOrdrAmt",
        "goodsUv",
        "goodsPv",
        "payOrdrUsrCntYtd",
        "payOrdrCntYtd",
        "payOrdrGoodsQtyYtd",
        "payOrdrAmtYtd",
        "goodsUvYtd",
        "goodsPvYtd",
    }
)
READY_REQUEST_KEYS = frozenset({"crawlerInfo"})
RESPONSE_KEYS = frozenset({"success", "errorCode", "errorMsg", "result"})
METRIC_FIELDS = (
    "payOrdrUsrCnt",
    "payOrdrCnt",
    "payOrdrGoodsQty",
    "payOrdrAmt",
    "goodsUv",
    "goodsPv",
)
YTD_METRIC_FIELDS = tuple(f"{field}Ytd" for field in METRIC_FIELDS)
ALL_METRIC_FIELDS = frozenset((*METRIC_FIELDS, *YTD_METRIC_FIELDS))
DOM_LABELS = {
    "paid_buyer_count": "支付买家数",
    "paid_order_count": "支付订单数",
    "paid_quantity": "支付件数",
    "paid_product_quantity": "支付商品件数",
    "paid_amount": "支付金额",
    "paid_amount_yuan": "支付金额(元)",
    "product_visitors": "商品访客数",
    "product_page_views": "商品浏览量",
}
COMPARISON_RISK_PHRASES = {
    "previous_same_period": "昨日同期",
    "previous_as_of": "昨日截至",
    "same_period_as_previous": "与昨日同一时段",
    "as_of_previous": "截至昨日",
    "data_update_time": "数据更新时间",
    "data_updated_through": "数据更新至",
    "data_cutoff": "数据截止",
    "previous_full_day": "昨日全天",
}
PRODUCT_CONTROL_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "last_7_days_tian": "近7天",
    "last_7_days_ri": "近7日",
    "traffic": "流量",
    "transaction": "交易",
    "all_metrics": "全部指标",
    "custom_metrics": "自定义指标",
    "metrics": "指标",
}

_CONTROL_TAG_CATEGORIES = frozenset(
    {"A", "BUTTON", "DIV", "INPUT", "LABEL", "LI", "SELECT", "SPAN", "OTHER"}
)
_CONTROL_ROLE_CATEGORIES = frozenset(
    {
        "NONE",
        "BUTTON",
        "TAB",
        "OPTION",
        "COMBOBOX",
        "MENUITEM",
        "RADIO",
        "CHECKBOX",
        "LINK",
        "OTHER",
    }
)
_CONTROL_INTERACTION_CATEGORIES = frozenset(
    {
        "NATIVE_LINK",
        "NATIVE_BUTTON",
        "NATIVE_INPUT",
        "NATIVE_SELECT",
        "ROLE_LINK",
        "ROLE_BUTTON",
        "ROLE_TAB",
        "ROLE_OPTION",
        "ROLE_COMBOBOX",
        "ROLE_MENUITEM",
        "ROLE_RADIO",
        "ROLE_CHECKBOX",
        "NONNEGATIVE_TABINDEX",
        "CURSOR_POINTER_ONLY",
    }
)
_PLACEHOLDER_CATEGORIES = frozenset(
    {
        "MISSING",
        "EMPTY",
        "DATE_START",
        "DATE_END",
        "DATE_SELECT",
        "PRODUCT_ID_SEARCH",
        "PRODUCT_NAME_SEARCH",
        "PRODUCT_ID_OR_NAME_SEARCH",
        "OTHER",
    }
)
_INPUT_VALUE_CATEGORIES = frozenset({"EMPTY", "ISO_DATE", "ISO_DATE_RANGE", "OTHER"})
_ARIA_HASPOPUP_CATEGORIES = frozenset(
    {"MISSING", "EMPTY", "TRUE", "FALSE", "MENU", "LISTBOX", "TREE", "GRID", "DIALOG", "OTHER"}
)
_POINTER_ANCESTOR_CATEGORIES = _CONTROL_INTERACTION_CATEGORIES | {"CURSOR_POINTER_ONLY"}
_DATE_FORMAT_CODES = frozenset(
    {"YYYY_MM_DD_HYPHEN", "YYYY_MM_DD_SLASH", "YYYY_MM_DD_DOT", "MM_DD", "M_MONTH_D_DAY"}
)
_DATE_SUBJECT_CODES = frozenset({"CURRENT_DATE", "PREVIOUS_DATE"})
_DATE_RANGE_CODES = frozenset({"CURRENT_TO_CURRENT", "PREVIOUS_TO_PREVIOUS"})
_VISIBLE_TEXT_INPUT_KEYS = frozenset(
    {
        "index",
        "type",
        "readonly",
        "disabled",
        "enabled",
        "value_category",
        "placeholder_category",
        "aria_haspopup_category",
        "pointer_ancestor_found",
        "pointer_ancestor_depth",
        "pointer_ancestor_tag",
        "pointer_ancestor_category",
        "sibling_calendar_icon_present",
        "sibling_calendar_icon_count",
    }
)

_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ID_CANDIDATE = re.compile(r"(?<!\d)(?:\d[\s\-_.:]*){5,20}(?!\d)")
_NUMERIC_TEXT = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_GROUPED_NUMERIC_TEXT = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_PERCENT_TEXT = re.compile(
    r"^-?(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)[%\uFF05]$"
)
_LESS_THAN_NUMERIC_TEXT = re.compile(
    r"^<(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)$"
)
_GREATER_THAN_NUMERIC_TEXT = re.compile(
    r"^>(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)$"
)
_COUNT_UNIT_SUFFIX_TEXT = re.compile(
    r"^-?(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?:人|单|件|次)$"
)
_YUAN_SUFFIX_TEXT = re.compile(r"^-?(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)元$")
_CNY_SYMBOL_PREFIX_TEXT = re.compile(
    r"^[\u00A5\uFFE5]-?(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)$"
)
_RANGE_NUMBER = r"(?:(?:0|[1-9]\d*)(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)"
_NUMERIC_RANGE_TEXT = re.compile(rf"^{_RANGE_NUMBER}\s*(?:~|\uFF5E|-|至)\s*{_RANGE_NUMBER}$")
_RANGE_WITH_UNIT_TEXT = re.compile(
    rf"^(?:{_RANGE_NUMBER}\s*(?:~|\uFF5E|-|至)\s*{_RANGE_NUMBER})(?:人|单|件|元)$"
)
_PLACEHOLDER_OTHER_TEXT = re.compile(r"^(?:N/?A|NULL|/|暂无|无数据)$", re.IGNORECASE)
_DASH_PLACEHOLDER_TEXT = re.compile(r"^[\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212]+$")
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
_DOM_LABEL_PROBE = r"""
payload => {
    const metricLabels = payload.metric_labels;
    const riskPhrases = payload.risk_phrases;
    const normalize = value => String(value)
        .normalize('NFKC')
        .replace(/[\s\u200b\ufeff]+/gu, '')
        .replace(/[\uFF1A:]+$/u, '');
    const count = labels => {
        const texts = [];
        for (const element of document.querySelectorAll('body *')) {
            const text = typeof element.innerText === 'string' ? element.innerText : '';
            if (text) texts.push(text);
        }
        const result = {};
        for (const [code, label] of Object.entries(labels)) {
            const wanted = normalize(label);
            let exact = 0;
            let normalized = 0;
            let contains = 0;
            for (const text of texts) {
                if (text === label) exact += 1;
                if (normalize(text) === wanted) normalized += 1;
                if (normalize(text).includes(wanted)) contains += 1;
            }
            result[code] = {
                exact_count: exact,
                normalized_count: normalized,
                normalized_contains_count: contains,
            };
        }
        return result;
    };
    return {
        current: count(metricLabels),
        risk_phrases: count(riskPhrases),
    };
}
"""

_PRODUCT_CONTROL_INVENTORY_PROBE = r"""
payload => {
    const labels = payload.labels;
    const dateCandidates = payload.date_candidates;
    const rangeCandidates = payload.range_candidates;
    const tags = new Set(['A', 'BUTTON', 'DIV', 'INPUT', 'LABEL', 'LI', 'SELECT', 'SPAN']);
    const roles = new Set([
        'button', 'tab', 'option', 'combobox', 'menuitem', 'radio', 'checkbox', 'link'
    ]);
    const normalize = value => String(value)
        .normalize('NFKC')
        .replace(/[\s\u200b\ufeff]+/gu, '');
    const bump = (target, key) => { target[key] = (target[key] || 0) + 1; };
    const visible = element => {
        const style = getComputedStyle(element);
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && element.getClientRects().length > 0;
    };
    const descriptor = element => {
        const rawTag = String(element.tagName || '').toUpperCase();
        const tag = tags.has(rawTag) ? rawTag : 'OTHER';
        const rawRole = String(element.getAttribute('role') || '').trim().toLowerCase();
        const role = rawRole ? (roles.has(rawRole) ? rawRole.toUpperCase() : 'OTHER') : 'NONE';
        let category = null;
        if (rawTag === 'A' && element.hasAttribute('href')) category = 'NATIVE_LINK';
        else if (rawTag === 'BUTTON') category = 'NATIVE_BUTTON';
        else if (rawTag === 'INPUT') category = 'NATIVE_INPUT';
        else if (rawTag === 'SELECT') category = 'NATIVE_SELECT';
        else if (roles.has(rawRole)) category = `ROLE_${rawRole.toUpperCase()}`;
        else {
            const tabIndex = element.getAttribute('tabindex');
            if (tabIndex !== null && /^\d+$/.test(tabIndex) && Number(tabIndex) >= 0) {
                category = 'NONNEGATIVE_TABINDEX';
            }
        }
        return category === null ? null : {tag, role, category};
    };
    const empty = () => ({
        exact_normalized_element_count: 0,
        visible_element_count: 0,
        direct_interactive_count: 0,
        direct_interactive_tag_counts: {},
        direct_interactive_role_counts: {},
        direct_interactive_category_counts: {},
        nearest_interactive_ancestor_count: 0,
        ancestor_depth_counts: {},
        ancestor_tag_counts: {},
        ancestor_role_counts: {},
        ancestor_category_counts: {},
    });
    const allElements = Array.from(document.querySelectorAll('body *'));
    const scanTruncated = allElements.length > 50000;
    const elements = allElements.slice(0, 50000);
    const controls = {};
    for (const [code, label] of Object.entries(labels)) {
        const wanted = normalize(label);
        const summary = empty();
        for (const element of elements) {
            const text = typeof element.textContent === 'string' ? element.textContent : '';
            if (normalize(text) !== wanted) continue;
            summary.exact_normalized_element_count += 1;
            if (visible(element)) summary.visible_element_count += 1;
            const direct = descriptor(element);
            if (direct !== null) {
                summary.direct_interactive_count += 1;
                bump(summary.direct_interactive_tag_counts, direct.tag);
                bump(summary.direct_interactive_role_counts, direct.role);
                bump(summary.direct_interactive_category_counts, direct.category);
            }
            let ancestor = element.parentElement;
            for (let depth = 1; depth <= 4 && ancestor !== null; depth += 1) {
                const found = descriptor(ancestor);
                if (found !== null) {
                    summary.nearest_interactive_ancestor_count += 1;
                    bump(summary.ancestor_depth_counts, String(depth));
                    bump(summary.ancestor_tag_counts, found.tag);
                    bump(summary.ancestor_role_counts, found.role);
                    bump(summary.ancestor_category_counts, found.category);
                    break;
                }
                ancestor = ancestor.parentElement;
            }
        }
        controls[code] = summary;
    }
    const pointerOrInteractive = element => {
        const found = descriptor(element);
        if (found !== null) return found;
        if (getComputedStyle(element).cursor !== 'pointer') return null;
        const rawTag = String(element.tagName || '').toUpperCase();
        return {
            tag: tags.has(rawTag) ? rawTag : 'OTHER',
            role: 'NONE',
            category: 'CURSOR_POINTER_ONLY',
        };
    };
    const summarizeMatches = predicate => {
        const summary = empty();
        for (const element of elements) {
            const text = typeof element.textContent === 'string' ? element.textContent : '';
            if (!predicate(normalize(text))) continue;
            summary.exact_normalized_element_count += 1;
            if (visible(element)) summary.visible_element_count += 1;
            const direct = pointerOrInteractive(element);
            if (direct !== null) {
                summary.direct_interactive_count += 1;
                bump(summary.direct_interactive_tag_counts, direct.tag);
                bump(summary.direct_interactive_role_counts, direct.role);
                bump(summary.direct_interactive_category_counts, direct.category);
            }
            let ancestor = element.parentElement;
            for (let depth = 1; depth <= 4 && ancestor !== null; depth += 1) {
                const found = pointerOrInteractive(ancestor);
                if (found !== null) {
                    summary.nearest_interactive_ancestor_count += 1;
                    bump(summary.ancestor_depth_counts, String(depth));
                    bump(summary.ancestor_tag_counts, found.tag);
                    bump(summary.ancestor_role_counts, found.role);
                    bump(summary.ancestor_category_counts, found.category);
                    break;
                }
                ancestor = ancestor.parentElement;
            }
        }
        return summary;
    };
    const dateMarkers = {};
    for (const [subject, formats] of Object.entries(dateCandidates)) {
        const subjectResult = {};
        for (const [format, candidate] of Object.entries(formats)) {
            const wanted = normalize(candidate);
            subjectResult[format] = {
                exact: summarizeMatches(text => text === wanted),
                contains: summarizeMatches(text => text.includes(wanted)),
            };
        }
        dateMarkers[subject] = subjectResult;
    }
    const dateRanges = {};
    for (const [category, candidates] of Object.entries(rangeCandidates)) {
        const wanted = new Set(candidates.map(normalize));
        dateRanges[category] = {
            exact: summarizeMatches(text => wanted.has(text)),
            contains: summarizeMatches(
                text => Array.from(wanted).some(item => text.includes(item))
            ),
        };
    }
    const placeholderCategory = element => {
        const raw = element.getAttribute('placeholder');
        if (raw === null) return 'MISSING';
        const value = normalize(raw);
        if (!value) return 'EMPTY';
        if (value === '开始日期' || value === '开始时间') return 'DATE_START';
        if (value === '结束日期' || value === '结束时间') return 'DATE_END';
        if (
            value === '选择日期'
            || value === '请选择日期'
            || value === '选择时间'
            || value === '请选择时间'
        ) return 'DATE_SELECT';
        if (value === '请输入商品ID') return 'PRODUCT_ID_SEARCH';
        if (value === '请输入商品名称') return 'PRODUCT_NAME_SEARCH';
        if (
            value === '请输入商品名称或ID'
            || value === '请输入商品名称/ID'
            || value === '请输入商品ID或名称'
        ) return 'PRODUCT_ID_OR_NAME_SEARCH';
        return 'OTHER';
    };
    const validIsoDate = value => {
        const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
        if (match === null) return false;
        const year = Number(match[1]);
        const month = Number(match[2]);
        const day = Number(match[3]);
        const date = new Date(Date.UTC(year, month - 1, day));
        return date.getUTCFullYear() === year
            && date.getUTCMonth() === month - 1
            && date.getUTCDate() === day;
    };
    const valueCategory = raw => {
        const value = String(raw || '').trim();
        if (!value) return 'EMPTY';
        if (validIsoDate(value)) return 'ISO_DATE';
        const range = /^(\d{4}-\d{2}-\d{2})(?:\s+[-~]\s+|\s*至\s*)(\d{4}-\d{2}-\d{2})$/.exec(value);
        if (range !== null && validIsoDate(range[1]) && validIsoDate(range[2])) {
            return 'ISO_DATE_RANGE';
        }
        return 'OTHER';
    };
    const haspopupCategory = element => {
        const raw = element.getAttribute('aria-haspopup');
        if (raw === null) return 'MISSING';
        const value = String(raw).trim().toLowerCase();
        if (!value) return 'EMPTY';
        if (['true', 'false', 'menu', 'listbox', 'tree', 'grid', 'dialog'].includes(value)) {
            return value.toUpperCase();
        }
        return 'OTHER';
    };
    const pointerAncestor = element => {
        let ancestor = element.parentElement;
        for (let depth = 1; depth <= 4 && ancestor !== null; depth += 1) {
            if (getComputedStyle(ancestor).cursor === 'pointer') {
                const found = descriptor(ancestor);
                const rawTag = String(ancestor.tagName || '').toUpperCase();
                return {
                    found: true,
                    depth,
                    tag: tags.has(rawTag) ? rawTag : 'OTHER',
                    category: found === null ? 'CURSOR_POINTER_ONLY' : found.category,
                };
            }
            ancestor = ancestor.parentElement;
        }
        return {found: false, depth: null, tag: null, category: null};
    };
    const calendarIconCount = element => {
        const parent = element.parentElement;
        if (parent === null) return 0;
        let count = 0;
        for (const sibling of Array.from(parent.children)) {
            if (sibling === element) continue;
            const rawTag = String(sibling.tagName || '').toUpperCase();
            const iconLike = rawTag === 'I'
                || rawTag === 'SVG'
                || sibling.querySelector('i,svg') !== null;
            if (!iconLike) continue;
            const classValue = typeof sibling.className === 'string'
                ? sibling.className
                : String(sibling.getAttribute('class') || '');
            const tokens = [
                classValue,
                sibling.getAttribute('aria-label') || '',
                sibling.getAttribute('title') || '',
                sibling.getAttribute('data-testid') || '',
            ].join(' ').normalize('NFKC').toLowerCase();
            if (/(?:calendar|date[-_ ]?picker|日历|日期)/u.test(tokens)) count += 1;
        }
        return count;
    };
    const inputs = {
        date: {element_count: 0, visible_count: 0, placeholder_category_counts: {}},
        text: {element_count: 0, visible_count: 0, placeholder_category_counts: {}},
    };
    const allInputs = Array.from(document.querySelectorAll('input'));
    const inputScanTruncated = allInputs.length > 5000;
    const visibleTextInputDetails = [];
    let visibleTextInputTruncated = false;
    for (const element of allInputs.slice(0, 5000)) {
        const rawType = String(element.getAttribute('type') || 'text').trim().toLowerCase();
        if (rawType !== 'date' && rawType !== 'text') continue;
        const target = inputs[rawType];
        target.element_count += 1;
        const isVisible = visible(element);
        if (isVisible) target.visible_count += 1;
        bump(target.placeholder_category_counts, placeholderCategory(element));
        if (rawType === 'text' && isVisible) {
            if (visibleTextInputDetails.length >= 20) {
                visibleTextInputTruncated = true;
                continue;
            }
            const pointer = pointerAncestor(element);
            const calendarCount = calendarIconCount(element);
            visibleTextInputDetails.push({
                index: visibleTextInputDetails.length,
                type: 'TEXT',
                readonly: element.readOnly === true,
                disabled: element.disabled === true,
                enabled: element.disabled !== true,
                value_category: valueCategory(element.value),
                placeholder_category: placeholderCategory(element),
                aria_haspopup_category: haspopupCategory(element),
                pointer_ancestor_found: pointer.found,
                pointer_ancestor_depth: pointer.depth,
                pointer_ancestor_tag: pointer.tag,
                pointer_ancestor_category: pointer.category,
                sibling_calendar_icon_present: calendarCount > 0,
                sibling_calendar_icon_count: calendarCount,
            });
        }
    }
    return {
        controls,
        date_markers: dateMarkers,
        date_ranges: dateRanges,
        inputs,
        visible_text_inputs: visibleTextInputDetails,
        visible_text_input_truncated: visibleTextInputTruncated,
        scanned_element_count: elements.length,
        scan_truncated: scanTruncated,
        input_scan_truncated: inputScanTruncated,
    };
}
"""

type ErrorCode = Literal[
    "READ_ONLY_CONFIRMATION_REQUIRED",
    "INCOMPATIBLE_MODE_FLAGS",
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
    "IDENTITY_STATE_UNAVAILABLE",
    "IDENTITY_UNVERIFIED",
    "IDENTITY_MISMATCH",
    "TARGET_RELOAD_FAILED",
    "TARGET_PAGE_CHANGED",
    "CAPTURE_TIMEOUT",
    "TAB_CONTROL_CHECK_FAILED",
    "TAB_SWITCH_DISABLED_GLOBAL_ROUTE",
    "TRAFFIC_TAB_NOT_UNIQUE",
    "TRANSACTION_TAB_NOT_UNIQUE",
    "TAB_CONTROL_NOT_INTERACTIVE",
    "TAB_CONTROL_HREF_UNSAFE",
    "INITIAL_TRAFFIC_STATE_UNVERIFIED",
    "TRANSACTION_TAB_RESPONSE_MISSING",
    "TRANSACTION_TAB_CLICK_FAILED",
    "TRAFFIC_TAB_RESTORE_FAILED",
    "RESTORE_TRAFFIC_UNVERIFIED",
    "CANDIDATE_OBSERVATION_LIMIT_EXCEEDED",
    "CONTENT_LENGTH_INVALID",
    "RESPONSE_TOO_LARGE",
    "REQUEST_JSON_UNAVAILABLE",
    "REQUEST_CONTRACT_MISMATCH",
    "REQUEST_VALUE_INVALID",
    "DUPLICATE_JSON_KEY",
    "NON_FINITE_JSON_NUMBER",
    "INVALID_JSON_RESPONSE",
    "RESPONSE_CONTRACT_MISMATCH",
    "BUSINESS_RESPONSE_NOT_SUCCESS",
    "RESULT_CONTRACT_MISMATCH",
    "ROW_CONTRACT_MISMATCH",
    "TIMESTAMP_UNPARSEABLE",
    "READY_DATE_UNPARSEABLE",
    "STAT_DATE_UNPARSEABLE",
    "TOTAL_COUNT_INVALID",
    "CAPTURED_EXCEEDS_TOTAL",
    "CATALOG_LOCK_BUSY",
    "CATALOG_RUNTIME_UNAVAILABLE",
    "CATALOG_SNAPSHOT_INVALID",
    "CATALOG_SNAPSHOT_NOT_FOUND",
    "CATALOG_STORAGE_INVALID",
    "CATALOG_STORE_MISMATCH",
    "PRODUCT_ID_INVALID",
    "DOM_LABEL_PROBE_FAILED",
    "DOM_LABEL_RESULT_INVALID",
    "PRODUCT_CONTROL_PROBE_FAILED",
    "PRODUCT_CONTROL_RESULT_INVALID",
    "DOM_SCROLL_RESTORE_FAILED",
    "REQUIRED_RESPONSE_MISSING",
    "SEMANTIC_OBSERVATION_AMBIGUOUS",
    "SEMANTIC_CAPTURE_FAILED",
    "PLAYWRIGHT_STOP_FAILED",
    "UNEXPECTED_FAILURE",
]

type TabHrefCategory = Literal[
    "MISSING",
    "EMPTY",
    "HASH_FRAGMENT",
    "JAVASCRIPT_VOID_0_EXACT",
    "JAVASCRIPT",
    "SAME_ORIGIN_SAME_PATH",
    "SAME_ORIGIN_OTHER_PATH",
    "EXTERNAL",
    "INVALID",
]

_SAFE_TAB_HREF_CATEGORIES = frozenset(
    {"MISSING", "EMPTY", "HASH_FRAGMENT", "SAME_ORIGIN_SAME_PATH"}
)

_GATE_SELECTORS: tuple[tuple[ErrorCode, tuple[str, ...]], ...] = (
    ("LOGIN_REQUIRED", _LOGIN_SELECTORS),
    ("CAPTCHA_PRESENT", _CAPTCHA_SELECTORS),
    ("RISK_CONTROL_PRESENT", _RISK_SELECTORS),
)


@dataclass(frozen=True)
class SemanticsRuntime:
    cdp_endpoint: str
    connect_timeout_ms: int
    collection_timeout_ms: int
    expected_store_id: str
    expected_store_id_sha256: str
    catalog_storage: StorageSettings | None = None
    catalog_allowed_store_ids: frozenset[str] = frozenset()
    catalog_store_id: str = ""
    catalog_max_response_bytes: int = 0


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
    list_candidate_response_events: int = 0
    ready_candidate_response_events: int = 0
    request_json_reads: int = 0
    response_body_reads: int = 0
    identity_dom_reads: int = 0
    dom_label_probe_runs: int = 0
    temporary_scroll_probe_runs: int = 0
    product_control_probe_runs: int = 0
    tab_control_checks: int = 0
    read_only_tab_clicks: int = 0
    traffic_restore_clicks: int = 0
    safety_selector_checks: int = 0
    listener_removals: int = 0
    scalar_metric_value_outputs: int = 0
    product_id_outputs: int = 0
    product_name_outputs: int = 0
    raw_body_outputs: int = 0
    header_outputs: int = 0
    full_url_outputs: int = 0
    playwright_stop_attempts: int = 0


class SemanticsStopped(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        counts: SafetyCounts | None = None,
        *,
        safe_detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.counts = counts or SafetyCounts()
        self.safe_detail = safe_detail


def _checkpoint(code: str) -> None:
    """Emit one fixed, non-business diagnostic stage to stderr."""
    if code not in _CHECKPOINT_CODES:
        raise AssertionError("unknown diagnostic checkpoint")
    print(f"PDD_PRODUCT_BUSINESS_SEMANTICS:{code}", file=sys.stderr, flush=True)


def _date_formats(value: date) -> dict[str, str]:
    return {
        "YYYY_MM_DD_HYPHEN": value.isoformat(),
        "YYYY_MM_DD_SLASH": value.strftime("%Y/%m/%d"),
        "YYYY_MM_DD_DOT": value.strftime("%Y.%m.%d"),
        "MM_DD": value.strftime("%m-%d"),
        "M_MONTH_D_DAY": f"{value.month}月{value.day}日",
    }


def _date_control_candidates(current_date: date) -> dict[str, object]:
    previous_date = current_date - timedelta(days=1)
    current = _date_formats(current_date)
    previous = _date_formats(previous_date)

    def ranges(values: dict[str, str]) -> list[str]:
        return [
            rendered
            for value in values.values()
            for rendered in (f"{value} - {value}", f"{value} ~ {value}", f"{value}至{value}")
        ]

    return {
        "labels": PRODUCT_CONTROL_LABELS,
        "date_candidates": {
            "CURRENT_DATE": current,
            "PREVIOUS_DATE": previous,
        },
        "range_candidates": {
            "CURRENT_TO_CURRENT": ranges(current),
            "PREVIOUS_TO_PREVIOUS": ranges(previous),
        },
    }


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


def _load_runtime(config_path: Path, connection_id: str) -> SemanticsRuntime:
    if _CONNECTION_ID.fullmatch(connection_id) is None:
        raise SemanticsStopped("INVALID_CONNECTION_ID")
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise SemanticsStopped("CONFIG_LOAD_FAILED") from exc
    try:
        connection = config.connection(connection_id)
    except KeyError as exc:
        raise SemanticsStopped("CONNECTION_ID_NOT_FOUND") from exc
    if config.service.test_mode:
        raise SemanticsStopped("REAL_MODE_REQUIRED")
    if not connection.real_collection_enabled:
        raise SemanticsStopped("REAL_COLLECTION_NOT_ENABLED")
    if connection.cdp_endpoint is None:
        raise SemanticsStopped("CDP_ENDPOINT_NOT_CONFIGURED")
    if not _is_trusted_loopback_endpoint(connection.cdp_endpoint):
        raise SemanticsStopped("CDP_ENDPOINT_NOT_TRUSTED")
    if bool(connection.expected_platform_store_id) == bool(
        connection.expected_platform_store_id_sha256
    ):
        raise SemanticsStopped("EXPECTED_IDENTITY_NOT_CONFIGURED")
    identity_page = connection.product_catalog_adapter.identity_page_url
    if not _matches_exact_page(identity_page, IDENTITY_PAGE_URL):
        raise SemanticsStopped("IDENTITY_PAGE_CONFIG_MISMATCH")
    return SemanticsRuntime(
        cdp_endpoint=connection.cdp_endpoint,
        connect_timeout_ms=config.collection.connect_timeout_ms,
        collection_timeout_ms=config.collection.collection_timeout_ms,
        expected_store_id=connection.expected_platform_store_id,
        expected_store_id_sha256=connection.expected_platform_store_id_sha256,
        catalog_storage=config.storage,
        catalog_allowed_store_ids=config.allowed_store_ids,
        catalog_store_id=connection.store_id,
        catalog_max_response_bytes=config.collection.max_mcp_response_bytes,
    )


def _read_latest_active_product_catalog(
    repository: LocalFileSnapshotRepository,
    *,
    store_id: str,
) -> tuple[frozenset[str], dict[str, object]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    selected: object | None = None
    while True:
        page = repository.list_snapshots(
            store_id=store_id,
            dataset_type=DatasetType.PRODUCT_CATALOG,
            captured_from=None,
            captured_to=None,
            cursor=cursor,
            limit=100,
        )
        for item in page.items:
            if item.store_id != store_id:
                raise SemanticsStopped("CATALOG_STORE_MISMATCH")
            if item.dataset_type is not DatasetType.PRODUCT_CATALOG:
                raise SemanticsStopped("CATALOG_SNAPSHOT_INVALID")
            if item.effective_status == "ACTIVE":
                selected = item
                break
        if selected is not None:
            break
        cursor = page.next_cursor
        if cursor is None:
            raise SemanticsStopped("CATALOG_SNAPSHOT_NOT_FOUND")
        if cursor in seen_cursors:
            raise SemanticsStopped("CATALOG_STORAGE_INVALID")
        seen_cursors.add(cursor)

    latest = cast(Any, selected)
    if (
        latest.quality_status != "VALID"
        or latest.source != "PDD_BROWSER_CDP"
        or latest.coverage.value != "COMPLETE"
        or isinstance(latest.record_count, bool)
        or not isinstance(latest.record_count, int)
        or latest.record_count < 1
    ):
        raise SemanticsStopped("CATALOG_SNAPSHOT_INVALID")

    read_cursor: str | None = None
    seen_read_cursors: set[str] = set()
    record_count = 0
    product_ids: set[str] = set()
    while True:
        read = repository.read_snapshot(
            snapshot_id=latest.snapshot_id,
            cursor=read_cursor,
            page_size=200,
        )
        manifest = read.manifest
        if manifest.get("store_id") != store_id:
            raise SemanticsStopped("CATALOG_STORE_MISMATCH")
        if (
            read.snapshot_id != latest.snapshot_id
            or read.effective_status != "ACTIVE"
            or read.invalidation is not None
            or manifest.get("snapshot_id") != latest.snapshot_id
            or manifest.get("dataset_type") != DatasetType.PRODUCT_CATALOG.value
            or manifest.get("source") != "PDD_BROWSER_CDP"
            or manifest.get("record_count") != latest.record_count
            or read.data is not None
        ):
            raise SemanticsStopped("CATALOG_SNAPSHOT_INVALID")
        for record in read.records:
            platform_product_id = record.get("platform_product_id")
            if (
                not isinstance(platform_product_id, str)
                or re.fullmatch(r"[1-9][0-9]{0,127}", platform_product_id) is None
            ):
                raise SemanticsStopped("CATALOG_SNAPSHOT_INVALID")
            product_ids.add(platform_product_id)
        record_count += len(read.records)
        next_cursor = read.next_cursor
        if next_cursor is None:
            break
        if next_cursor in seen_read_cursors:
            raise SemanticsStopped("CATALOG_STORAGE_INVALID")
        seen_read_cursors.add(next_cursor)
        read_cursor = next_cursor

    if record_count != latest.record_count or not product_ids:
        raise SemanticsStopped("CATALOG_SNAPSHOT_INVALID")
    return frozenset(product_ids), {
        "requested": True,
        "latest_active_selected": True,
        "store_binding_matched": True,
        "dataset_matched": True,
        "quality_valid": True,
        "real_browser_source": True,
        "coverage_complete": True,
        "manifest_record_count": latest.record_count,
        "records_read_count": record_count,
        "unique_platform_product_id_count": len(product_ids),
        "pagination_complete": True,
        "snapshot_id_output": False,
        "store_id_output": False,
        "platform_product_id_values_output": False,
        "product_name_fields_accessed": False,
        "non_id_record_fields_accessed": False,
    }


def _load_latest_product_catalog(
    runtime: SemanticsRuntime,
) -> tuple[frozenset[str], dict[str, object]]:
    if (
        runtime.catalog_storage is None
        or not runtime.catalog_store_id
        or runtime.catalog_store_id not in runtime.catalog_allowed_store_ids
        or runtime.catalog_max_response_bytes < 1
    ):
        raise SemanticsStopped("CATALOG_RUNTIME_UNAVAILABLE")
    repository = LocalFileSnapshotRepository(
        runtime.catalog_storage,
        allowed_store_ids=runtime.catalog_allowed_store_ids,
        max_response_bytes=runtime.catalog_max_response_bytes,
    )
    try:
        with repository.service_lock(timeout=0):
            repository.initialize()
            return _read_latest_active_product_catalog(
                repository,
                store_id=runtime.catalog_store_id,
            )
    except SemanticsStopped:
        raise
    except LockBusyError as exc:
        raise SemanticsStopped("CATALOG_LOCK_BUSY") from exc
    except AuthorizationError as exc:
        raise SemanticsStopped("CATALOG_STORE_MISMATCH") from exc
    except (SnapshotCorruptError, StorageError, ValidationFailure, ValueError) as exc:
        raise SemanticsStopped("CATALOG_STORAGE_INVALID") from exc


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


def _select_pages(browser: Browser) -> tuple[Page, Page, int, int, int]:
    contexts = list(browser.contexts)
    open_pages = [page for context in contexts for page in context.pages if not page.is_closed()]
    targets = [page for page in open_pages if _matches_exact_page(page.url, TARGET_PAGE_URL)]
    if not targets:
        raise SemanticsStopped("TARGET_PAGE_NOT_FOUND")
    if len(targets) != 1:
        raise SemanticsStopped("TARGET_PAGE_AMBIGUOUS")
    target = targets[0]
    identity_pages = [
        page for page in open_pages if _matches_exact_page(page.url, IDENTITY_PAGE_URL)
    ]
    if not identity_pages:
        raise SemanticsStopped("IDENTITY_PAGE_NOT_FOUND")
    if len(identity_pages) != 1:
        raise SemanticsStopped("IDENTITY_PAGE_AMBIGUOUS")
    identity_page = identity_pages[0]
    if identity_page.context is not target.context:
        raise SemanticsStopped("IDENTITY_PAGE_CONTEXT_MISMATCH")
    for required_url in REQUIRED_SAME_CONTEXT_PAGES:
        matches = [
            page
            for page in target.context.pages
            if not page.is_closed() and _matches_exact_page(page.url, required_url)
        ]
        if not matches:
            raise SemanticsStopped("REQUIRED_CONTEXT_PAGE_MISSING")
        if len(matches) != 1:
            raise SemanticsStopped("REQUIRED_CONTEXT_PAGE_AMBIGUOUS")
    return target, identity_page, len(contexts), len(open_pages), len(REQUIRED_SAME_CONTEXT_PAGES)


async def _assert_safety_gate(page: Page, counts: SafetyCounts) -> None:
    for code, selectors in _GATE_SELECTORS:
        for selector in selectors:
            counts.safety_selector_checks += 1
            try:
                present = await page.locator(selector).count()
            except Error as exc:
                raise SemanticsStopped("SEMANTIC_CAPTURE_FAILED", counts) from exc
            if present > 0:
                raise SemanticsStopped(code, counts)


async def _verify_identity(page: Page, runtime: SemanticsRuntime, counts: SafetyCounts) -> None:
    await _assert_safety_gate(page, counts)
    counts.identity_dom_reads += 1
    try:
        text = await page.locator("html").text_content()
    except Error as exc:
        raise SemanticsStopped("IDENTITY_STATE_UNAVAILABLE", counts) from exc
    if not isinstance(text, str) or not text:
        raise SemanticsStopped("IDENTITY_STATE_UNAVAILABLE", counts)
    matches: set[str] = set()
    candidate_seen = False
    for raw in _ID_CANDIDATE.findall(text):
        candidate = "".join(character for character in raw if character.isdigit())
        if not 5 <= len(candidate) <= 20:
            continue
        candidate_seen = True
        if runtime.expected_store_id:
            if candidate == runtime.expected_store_id:
                matches.add(candidate)
        elif (
            hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            == runtime.expected_store_id_sha256
        ):
            matches.add("matched-sha256")
    if len(matches) != 1:
        code: ErrorCode = "IDENTITY_MISMATCH" if candidate_seen else "IDENTITY_UNVERIFIED"
        raise SemanticsStopped(code, counts)


def _candidate_kind(response: Response) -> Literal["list", "ready"] | None:
    try:
        parsed = urlsplit(response.url)
        port = parsed.port
    except ValueError:
        return None
    content_type = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if (
        parsed.scheme != "https"
        or parsed.hostname != RESPONSE_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or response.request.method != "POST"
        or response.status != 200
        or content_type != "application/json"
    ):
        return None
    if parsed.path == LIST_RESPONSE_PATH:
        return "list"
    if parsed.path == READY_DATE_RESPONSE_PATH:
        return "ready"
    return None


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticsStopped("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise SemanticsStopped("NON_FINITE_JSON_NUMBER")


def _strict_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except SemanticsStopped:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise SemanticsStopped("INVALID_JSON_RESPONSE") from exc


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
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float | Decimal):
        return "number"
    return "other"


def _finite_number(value: object, *, code: ErrorCode = "REQUEST_VALUE_INVALID") -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise SemanticsStopped(code)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SemanticsStopped("NON_FINITE_JSON_NUMBER")
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise SemanticsStopped("NON_FINITE_JSON_NUMBER")
    return value


def _positive_integer(value: object) -> int:
    parsed = _finite_number(value)
    if isinstance(parsed, float) or parsed < 1:
        raise SemanticsStopped("REQUEST_VALUE_INVALID")
    return parsed


def _canonical_date(value: object, *, code: ErrorCode) -> date:
    if not isinstance(value, str):
        raise SemanticsStopped(code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SemanticsStopped(code) from exc
    if value != parsed.isoformat():
        raise SemanticsStopped(code)
    return parsed


def _crawler_info(value: object) -> None:
    if not isinstance(value, str) or len(value) > MAX_CRAWLER_INFO_LENGTH:
        raise SemanticsStopped("REQUEST_VALUE_INVALID")


def _strict_request_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or not all(isinstance(key, str) for key in value)
    ):
        raise SemanticsStopped("REQUEST_CONTRACT_MISMATCH")
    return cast(dict[str, object], value)


def _strict_response_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != RESPONSE_KEYS:
        raise SemanticsStopped("RESPONSE_CONTRACT_MISMATCH")
    payload = cast(dict[str, object], value)
    if payload.get("success") is not True:
        raise SemanticsStopped("BUSINESS_RESPONSE_NOT_SUCCESS")
    return payload


def _request_summary(value: object, *, kind: Literal["list", "ready"]) -> dict[str, object]:
    request = _strict_request_object(
        value, LIST_REQUEST_KEYS if kind == "list" else READY_REQUEST_KEYS
    )
    _crawler_info(request.get("crawlerInfo"))
    if kind == "ready":
        return {"contract_matched": True}
    start = _canonical_date(request.get("startDate"), code="REQUEST_VALUE_INVALID")
    end = _canonical_date(request.get("endDate"), code="REQUEST_VALUE_INVALID")
    if end < start:
        raise SemanticsStopped("REQUEST_VALUE_INVALID")
    return {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "pageNum": _positive_integer(request.get("pageNum")),
        "pageSize": _positive_integer(request.get("pageSize")),
        "queryType": _finite_number(request.get("queryType")),
        "sortCol": _finite_number(request.get("sortCol")),
        "sortType": _finite_number(request.get("sortType")),
        "actVs": _finite_number(request.get("actVs")),
    }


def _timestamp_summary(value: object) -> dict[str, object]:
    numeric = _finite_number(value, code="TIMESTAMP_UNPARSEABLE")
    if isinstance(numeric, float) and not numeric.is_integer():
        raise SemanticsStopped("TIMESTAMP_UNPARSEABLE")
    integer = int(numeric)
    absolute = abs(integer)
    if 946_684_800 <= absolute <= 4_102_444_800:
        divisor, unit = 1, "SECONDS"
    elif 946_684_800_000 <= absolute <= 4_102_444_800_000:
        divisor, unit = 1_000, "MILLISECONDS"
    elif 946_684_800_000_000 <= absolute <= 4_102_444_800_000_000:
        divisor, unit = 1_000_000, "MICROSECONDS"
    else:
        raise SemanticsStopped("TIMESTAMP_UNPARSEABLE")
    try:
        parsed = datetime.fromtimestamp(integer / divisor, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise SemanticsStopped("TIMESTAMP_UNPARSEABLE") from exc
    return {
        "json_type": _json_type(value),
        "epoch_unit": unit,
        "iso_utc": parsed.isoformat().replace("+00:00", "Z"),
        "interpretation": "OBSERVED_RESULT_TIMESTAMP_ONLY",
    }


def _parse_metric(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if _NUMERIC_TEXT.fullmatch(stripped) is None:
        return None
    try:
        parsed = Decimal(stripped)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _metric_format_category(value: object) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, str):
        return "NON_STRING"
    stripped = value.strip()
    if not stripped:
        return "BLANK"
    if _DASH_PLACEHOLDER_TEXT.fullmatch(stripped) is not None:
        return "DASH_PLACEHOLDER"
    if _NUMERIC_TEXT.fullmatch(stripped) is not None:
        return "PLAIN_NUMERIC"
    if _GROUPED_NUMERIC_TEXT.fullmatch(stripped) is not None:
        return "GROUPED_NUMERIC"
    if _NUMERIC_RANGE_TEXT.fullmatch(stripped) is not None:
        return "NUMERIC_RANGE"
    if _RANGE_WITH_UNIT_TEXT.fullmatch(stripped) is not None:
        return "RANGE_WITH_UNIT"
    if _PERCENT_TEXT.fullmatch(stripped) is not None:
        return "PERCENT"
    if _LESS_THAN_NUMERIC_TEXT.fullmatch(stripped) is not None:
        return "LESS_THAN_NUMERIC"
    if _GREATER_THAN_NUMERIC_TEXT.fullmatch(stripped) is not None:
        return "GREATER_THAN_NUMERIC"
    if _COUNT_UNIT_SUFFIX_TEXT.fullmatch(stripped) is not None:
        return "COUNT_UNIT_SUFFIX"
    if _YUAN_SUFFIX_TEXT.fullmatch(stripped) is not None:
        return "YUAN_SUFFIX"
    if _CNY_SYMBOL_PREFIX_TEXT.fullmatch(stripped) is not None:
        return "CNY_SYMBOL_PREFIX"
    if _PLACEHOLDER_OTHER_TEXT.fullmatch(stripped) is not None:
        return "PLACEHOLDER_OTHER"
    return "SUFFIXED_OR_OTHER"


_LEXICAL_FEATURES = (
    "has_digit",
    "has_cjk",
    "has_ascii_letter",
    "has_comma",
    "has_dot",
    "has_percent",
    "has_currency",
    "has_comparison",
    "has_unit",
    "has_asterisk",
    "has_slash",
    "has_dash",
    "has_whitespace",
    "has_other_punctuation",
    "has_private_use_codepoint",
    "has_non_bmp",
    "has_replacement_character",
    "ascii_hexlike",
    "base64like",
)
_KNOWN_LEXICAL_SYMBOLS = frozenset(
    ",\uff0c.\uff0e%\uff05¥￥<≤>≥*\uff0a/\uff0f-\u2010\u2011\u2012\u2013—―\u2212"
)


def _lexical_flags(value: str) -> dict[str, bool]:
    ascii_hexlike = len(value) >= 4 and re.fullmatch(r"[0-9A-Fa-f]+", value) is not None
    base64like = (
        len(value) >= 8
        and len(value) % 4 == 0
        and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value) is not None
    )
    return {
        "has_digit": any("0" <= char <= "9" for char in value),
        "has_cjk": any(
            "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff" for char in value
        ),
        "has_ascii_letter": any("A" <= char <= "Z" or "a" <= char <= "z" for char in value),
        "has_comma": any(char in {",", "\uff0c"} for char in value),
        "has_dot": any(char in {".", "\uff0e"} for char in value),
        "has_percent": any(char in {"%", "\uff05"} for char in value),
        "has_currency": any(char in {"¥", "￥", "元"} for char in value),
        "has_comparison": any(char in {"<", "≤", ">", "≥"} for char in value),
        "has_unit": any(char in {"人", "单", "件", "次", "万", "亿"} for char in value),
        "has_asterisk": any(char in {"*", "\uff0a"} for char in value),
        "has_slash": any(char in {"/", "\uff0f"} for char in value),
        "has_dash": any(
            char in {"-", "\u2010", "\u2011", "\u2012", "\u2013", "—", "―", "\u2212"}
            for char in value
        ),
        "has_whitespace": any(char.isspace() for char in value),
        "has_other_punctuation": any(
            (
                unicodedata.category(char).startswith("P")
                or unicodedata.category(char).startswith("S")
            )
            and char not in _KNOWN_LEXICAL_SYMBOLS
            for char in value
        ),
        "has_private_use_codepoint": any(
            0xE000 <= ord(char) <= 0xF8FF
            or 0xF0000 <= ord(char) <= 0xFFFFD
            or 0x100000 <= ord(char) <= 0x10FFFD
            for char in value
        ),
        "has_non_bmp": any(ord(char) > 0xFFFF for char in value),
        "has_replacement_character": "\ufffd" in value,
        "ascii_hexlike": ascii_hexlike,
        "base64like": base64like,
    }


def _lexical_signature(values: list[object]) -> dict[str, object]:
    strings = [value for value in values if isinstance(value, str)]
    length_histogram = Counter(len(value) for value in strings)
    feature_counts = Counter[str]()
    for string_value in strings:
        flags = _lexical_flags(string_value)
        for feature in _LEXICAL_FEATURES:
            if flags[feature]:
                feature_counts[feature] += 1
    unique_values: list[object] = []
    for raw_value in values:
        if not any(
            type(raw_value) is type(existing) and raw_value == existing
            for existing in unique_values
        ):
            unique_values.append(raw_value)
    return {
        "observed_count": len(values),
        "string_count": len(strings),
        "unique_value_count": len(unique_values),
        "string_length_histogram": {
            str(length): count for length, count in sorted(length_histogram.items())
        },
        "feature_true_counts": {feature: feature_counts[feature] for feature in _LEXICAL_FEATURES},
        "scalar_values_output": False,
        "value_hashes_output": False,
        "numeric_bounds_output": False,
    }


def _metric_summaries_for_fields(
    rows: list[dict[str, object]], fields: tuple[str, ...]
) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in fields:
        values = [row[field] for row in rows]
        parsed = [_parse_metric(value) for value in values]
        result[field] = {
            "type_counts": dict(sorted(Counter(_json_type(value) for value in values).items())),
            "format_category_counts": dict(
                sorted(Counter(_metric_format_category(value) for value in values).items())
            ),
            "parseable_count": sum(value is not None for value in parsed),
            "null_count": sum(value is None for value in values),
            "zero_count": sum(value == 0 for value in parsed if value is not None),
            "lexical_signature": _lexical_signature(values),
        }
    return result


def _metric_summaries(rows: list[dict[str, object]]) -> dict[str, object]:
    return _metric_summaries_for_fields(rows, METRIC_FIELDS)


def _current_ytd_pairing_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    pair_counts: dict[str, object] = {}
    fully_paired = 0
    for row in rows:
        if all(field in row and f"{field}Ytd" in row for field in METRIC_FIELDS):
            fully_paired += 1
    for field in METRIC_FIELDS:
        ytd_field = f"{field}Ytd"
        pair_counts[field] = {
            "current_present_count": sum(field in row for row in rows),
            "ytd_present_count": sum(ytd_field in row for row in rows),
            "paired_present_count": sum(field in row and ytd_field in row for row in rows),
        }
    return {
        "row_count": len(rows),
        "fully_paired_row_count": fully_paired,
        "incomplete_pair_row_count": len(rows) - fully_paired,
        "field_pair_presence_counts": pair_counts,
        "scalar_values_compared": False,
    }


def _row_contract_failure_detail(rows: list[dict[str, object]]) -> dict[str, object]:
    expected = set(LIST_ROW_KEYS)
    unknown_keys = sorted({key for row in rows for key in set(row) - expected})
    unknown_hashes = [
        hashlib.sha256(f"pdd-product-row-key:{key}".encode()).hexdigest()
        for key in unknown_keys[:64]
    ]
    key_count_histogram = Counter(len(row) for row in rows)
    missing_counts = [len(expected - set(row)) for row in rows]
    extra_counts = [len(set(row) - expected) for row in rows]
    known_fields: dict[str, object] = {}
    for field in sorted(LIST_ROW_KEYS):
        values = [row[field] for row in rows if field in row]
        detail: dict[str, object] = {
            "present_count": len(values),
            "missing_count": len(rows) - len(values),
            "type_counts": dict(sorted(Counter(_json_type(value) for value in values).items())),
        }
        if field in ALL_METRIC_FIELDS:
            parsed = [_parse_metric(value) for value in values]
            detail.update(
                {
                    "format_category_counts": dict(
                        sorted(Counter(_metric_format_category(value) for value in values).items())
                    ),
                    "parseable_count": sum(value is not None for value in parsed),
                    "null_count": sum(value is None for value in values),
                    "zero_count": sum(value == 0 for value in parsed if value is not None),
                }
            )
        known_fields[field] = detail
    return {
        "contract": "goodsDetailList_row",
        "row_count": len(rows),
        "expected_key_count": len(expected),
        "required_subset_matched_row_count": sum(expected.issubset(row) for row in rows),
        "missing_required_key_row_count": sum(not expected.issubset(row) for row in rows),
        "additional_key_row_count": sum(bool(set(row) - expected) for row in rows),
        "row_key_count_histogram": {
            str(count): frequency for count, frequency in sorted(key_count_histogram.items())
        },
        "missing_known_key_occurrences": sum(missing_counts),
        "extra_unknown_key_occurrences": sum(extra_counts),
        "unknown_key_unique_count": len(unknown_keys),
        "unknown_key_name_sha256": unknown_hashes,
        "unknown_key_hashes_truncated": len(unknown_keys) > len(unknown_hashes),
        "known_field_diagnostics": known_fields,
        "scalar_values_output": False,
        "product_ids_output": False,
        "product_names_output": False,
        "unknown_key_names_output": False,
    }


def _normalize_product_id(value: object) -> str:
    numeric = _finite_number(value, code="PRODUCT_ID_INVALID")
    if isinstance(numeric, float) or numeric < 0:
        raise SemanticsStopped("PRODUCT_ID_INVALID")
    return str(numeric)


def _id_summary(
    rows: list[dict[str, object]], catalog_product_ids: frozenset[str] | None
) -> dict[str, object]:
    normalized = [_normalize_product_id(row["goodsId"]) for row in rows]
    unique = set(normalized)
    matched = unique & set(catalog_product_ids) if catalog_product_ids is not None else None
    return {
        "present_count": len(normalized),
        "unique_count": len(unique),
        "duplicate_count": len(normalized) - len(unique),
        "d2_match_evaluated": catalog_product_ids is not None,
        "d2_match_count": len(matched) if matched is not None else None,
        "d2_unmatched_count": len(unique - set(catalog_product_ids))
        if catalog_product_ids is not None
        else None,
        "values_output": False,
    }


def _list_response_summary(
    value: object,
    *,
    request: dict[str, object],
    catalog_product_ids: frozenset[str] | None,
) -> dict[str, object]:
    payload = _strict_response_object(value)
    result_value = payload.get("result")
    if not isinstance(result_value, dict) or set(result_value) != LIST_RESULT_KEYS:
        raise SemanticsStopped("RESULT_CONTRACT_MISMATCH")
    result = cast(dict[str, object], result_value)
    _finite_number(result.get("delayData"), code="RESULT_CONTRACT_MISMATCH")
    rows_value = result.get("goodsDetailList")
    if not isinstance(rows_value, list) or not all(isinstance(row, dict) for row in rows_value):
        raise SemanticsStopped("RESULT_CONTRACT_MISMATCH")
    rows = cast(list[dict[str, object]], rows_value)
    if any(not LIST_ROW_KEYS.issubset(row) for row in rows):
        raise SemanticsStopped(
            "ROW_CONTRACT_MISMATCH",
            safe_detail=_row_contract_failure_detail(rows),
        )
    if any(not isinstance(row["goodsName"], str) for row in rows):
        raise SemanticsStopped(
            "ROW_CONTRACT_MISMATCH",
            safe_detail=_row_contract_failure_detail(rows),
        )
    for row in rows:
        try:
            _finite_number(row["goodsStatus"], code="ROW_CONTRACT_MISMATCH")
        except SemanticsStopped as exc:
            raise SemanticsStopped(
                "ROW_CONTRACT_MISMATCH",
                safe_detail=_row_contract_failure_detail(rows),
            ) from exc

    total_raw = _finite_number(result.get("totalNum"), code="TOTAL_COUNT_INVALID")
    if isinstance(total_raw, float) or total_raw < 0:
        raise SemanticsStopped("TOTAL_COUNT_INVALID")
    total = total_raw
    if len(rows) > total:
        raise SemanticsStopped("CAPTURED_EXCEEDS_TOTAL")
    start = _canonical_date(request.get("startDate"), code="REQUEST_VALUE_INVALID")
    end = _canonical_date(request.get("endDate"), code="REQUEST_VALUE_INVALID")
    stat_dates = [_canonical_date(row["statDate"], code="STAT_DATE_UNPARSEABLE") for row in rows]
    return {
        "coverage_counts": {
            "total": total,
            "captured": len(rows),
            "statDate_unique_count": len(set(stat_dates)),
            "statDate_parseable_count": len(stat_dates),
            "all_statDates_within_request": all(start <= item <= end for item in stat_dates),
        },
        "result_timestamp": _timestamp_summary(result.get("timestamp")),
        "product_ids": _id_summary(rows, catalog_product_ids),
        "metric_fields": _metric_summaries(rows),
        "ytd_suffix_fields": {
            "interpretation": "UNVERIFIED_COMPARISON_SUFFIX_ONLY",
            "previous_natural_day_semantics_verified": False,
            "ready_date_linkage_verified": False,
            "source_finalized_inferred": False,
            "fields": _metric_summaries_for_fields(rows, YTD_METRIC_FIELDS),
        },
        "current_ytd_pairing": _current_ytd_pairing_summary(rows),
    }


def _ready_response_summary(value: object) -> dict[str, object]:
    payload = _strict_response_object(value)
    ready = _canonical_date(payload.get("result"), code="READY_DATE_UNPARSEABLE")
    return {"readyDate": ready.isoformat()}


def _check_content_length(response: Response, counts: SafetyCounts) -> None:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise SemanticsStopped("CONTENT_LENGTH_INVALID", counts) from exc
        if declared < 0:
            raise SemanticsStopped("CONTENT_LENGTH_INVALID", counts)
        if declared > MAX_RESPONSE_BYTES:
            raise SemanticsStopped("RESPONSE_TOO_LARGE", counts)


async def _bounded_response_body(response: Response, counts: SafetyCounts) -> object:
    counts.response_body_reads += 1
    raw = await response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SemanticsStopped("RESPONSE_TOO_LARGE", counts)
    return _strict_json(raw)


async def _probe_candidate(
    response: Response,
    kind: Literal["list", "ready"],
    counts: SafetyCounts,
    catalog_product_ids: frozenset[str] | None,
) -> dict[str, object]:
    _check_content_length(response, counts)
    counts.request_json_reads += 1
    try:
        request_value = response.request.post_data_json
    except Error as exc:
        raise SemanticsStopped("REQUEST_JSON_UNAVAILABLE", counts) from exc
    request = _request_summary(request_value, kind=kind)
    response_value = await _bounded_response_body(response, counts)
    if kind == "ready":
        return {"request": request, **_ready_response_summary(response_value)}
    return {
        "request": request,
        **_list_response_summary(
            response_value,
            request=request,
            catalog_product_ids=catalog_product_ids,
        ),
    }


def _aggregate(observations: dict[str, list[dict[str, object]]]) -> dict[str, dict[str, object]]:
    listed = observations["list"]
    if not listed:
        raise SemanticsStopped("REQUIRED_RESPONSE_MISSING")
    if len(listed) != 1:
        raise SemanticsStopped("SEMANTIC_OBSERVATION_AMBIGUOUS")
    result = {"list": {"observation_count": 1, **listed[0]}}

    ready = observations["ready"]
    if not ready:
        result["ready"] = {
            "status": "NOT_OBSERVED_THIS_RELOAD",
            "observation_count": 0,
            "request": None,
            "readyDate": None,
        }
        return result
    signatures = {
        json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")) for item in ready
    }
    if len(signatures) != 1:
        raise SemanticsStopped("SEMANTIC_OBSERVATION_AMBIGUOUS")
    result["ready"] = {
        "status": "OBSERVED",
        "observation_count": len(ready),
        **ready[0],
    }
    return result


def _candidate_kind_counts(counts: SafetyCounts) -> dict[str, int]:
    return {
        "list": counts.list_candidate_response_events,
        "ready": counts.ready_candidate_response_events,
    }


def _validated_dom_group(value: object) -> dict[str, dict[str, int | bool]]:
    if not isinstance(value, dict) or set(value) != set(DOM_LABELS):
        raise SemanticsStopped("DOM_LABEL_RESULT_INVALID")
    result: dict[str, dict[str, int | bool]] = {}
    for field in DOM_LABELS:
        item = value.get(field)
        if not isinstance(item, dict) or set(item) != {
            "exact_count",
            "normalized_count",
            "normalized_contains_count",
        }:
            raise SemanticsStopped("DOM_LABEL_RESULT_INVALID")
        exact = item.get("exact_count")
        normalized = item.get("normalized_count")
        contains = item.get("normalized_contains_count")
        if (
            isinstance(exact, bool)
            or not isinstance(exact, int)
            or exact < 0
            or isinstance(normalized, bool)
            or not isinstance(normalized, int)
            or normalized < 0
            or isinstance(contains, bool)
            or not isinstance(contains, int)
            or contains < 0
        ):
            raise SemanticsStopped("DOM_LABEL_RESULT_INVALID")
        result[field] = {
            "exact_present": exact > 0,
            "exact_count": exact,
            "normalized_present": normalized > 0,
            "normalized_count": normalized,
            "normalized_contains_present": contains > 0,
            "normalized_contains_count": contains,
        }
    return result


def _validated_risk_phrase_group(value: object) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict) or set(value) != set(COMPARISON_RISK_PHRASES):
        raise SemanticsStopped("DOM_LABEL_RESULT_INVALID")
    result: dict[str, dict[str, int]] = {}
    for code in COMPARISON_RISK_PHRASES:
        item = value.get(code)
        if not isinstance(item, dict) or set(item) != {
            "exact_count",
            "normalized_count",
            "normalized_contains_count",
        }:
            raise SemanticsStopped("DOM_LABEL_RESULT_INVALID")
        counts = {
            key: item[key]
            for key in ("exact_count", "normalized_count", "normalized_contains_count")
        }
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in counts.values()
        ):
            raise SemanticsStopped("DOM_LABEL_RESULT_INVALID")
        result[code] = cast(dict[str, int], counts)
    return result


async def _dom_label_summary(page: Page, counts: SafetyCounts) -> dict[str, object]:
    counts.dom_label_probe_runs += 1
    task = asyncio.create_task(
        page.evaluate(
            _DOM_LABEL_PROBE,
            {
                "metric_labels": DOM_LABELS,
                "risk_phrases": COMPARISON_RISK_PHRASES,
            },
        )
    )
    try:
        done, pending = await asyncio.wait({task}, timeout=DOM_EVALUATE_TIMEOUT_SECONDS)
        if pending:
            task.cancel()
            task.add_done_callback(_consume_task_result)
            raise SemanticsStopped("DOM_LABEL_PROBE_FAILED", counts)
        raw: object = task.result()
    except SemanticsStopped:
        raise
    except Error as exc:
        raise SemanticsStopped("DOM_LABEL_PROBE_FAILED", counts) from exc
    if not isinstance(raw, dict) or set(raw) != {"current", "risk_phrases"}:
        raise SemanticsStopped("DOM_LABEL_RESULT_INVALID", counts)
    return {
        "current_dom": _validated_dom_group(raw.get("current")),
        "comparison_risk_phrase_counts": _validated_risk_phrase_group(raw.get("risk_phrases")),
        "comparison_risk_phrase_text_output": False,
        "temporary_horizontal_scroll_executed": False,
    }


def _validated_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    return value


def _validated_fixed_count_map(value: object, allowed: frozenset[str]) -> dict[str, int]:
    if not isinstance(value, dict) or not set(value).issubset(allowed):
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    return {key: _validated_count(value[key]) for key in sorted(value)}


def _validated_control_item(value: object) -> dict[str, object]:
    expected = {
        "exact_normalized_element_count",
        "visible_element_count",
        "direct_interactive_count",
        "direct_interactive_tag_counts",
        "direct_interactive_role_counts",
        "direct_interactive_category_counts",
        "nearest_interactive_ancestor_count",
        "ancestor_depth_counts",
        "ancestor_tag_counts",
        "ancestor_role_counts",
        "ancestor_category_counts",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    exact = _validated_count(value["exact_normalized_element_count"])
    visible = _validated_count(value["visible_element_count"])
    direct = _validated_count(value["direct_interactive_count"])
    ancestor = _validated_count(value["nearest_interactive_ancestor_count"])
    if visible > exact or direct > exact or ancestor > exact:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    return {
        "exact_normalized_element_count": exact,
        "visible_element_count": visible,
        "direct_interactive_count": direct,
        "direct_interactive_tag_counts": _validated_fixed_count_map(
            value["direct_interactive_tag_counts"], _CONTROL_TAG_CATEGORIES
        ),
        "direct_interactive_role_counts": _validated_fixed_count_map(
            value["direct_interactive_role_counts"], _CONTROL_ROLE_CATEGORIES
        ),
        "direct_interactive_category_counts": _validated_fixed_count_map(
            value["direct_interactive_category_counts"], _CONTROL_INTERACTION_CATEGORIES
        ),
        "nearest_interactive_ancestor_count": ancestor,
        "ancestor_depth_counts": _validated_fixed_count_map(
            value["ancestor_depth_counts"], frozenset({"1", "2", "3", "4"})
        ),
        "ancestor_tag_counts": _validated_fixed_count_map(
            value["ancestor_tag_counts"], _CONTROL_TAG_CATEGORIES
        ),
        "ancestor_role_counts": _validated_fixed_count_map(
            value["ancestor_role_counts"], _CONTROL_ROLE_CATEGORIES
        ),
        "ancestor_category_counts": _validated_fixed_count_map(
            value["ancestor_category_counts"], _CONTROL_INTERACTION_CATEGORIES
        ),
    }


def _validated_control_match_pair(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"exact", "contains"}:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    return {
        "exact": _validated_control_item(value["exact"]),
        "contains": _validated_control_item(value["contains"]),
    }


def _validated_input_item(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "element_count",
        "visible_count",
        "placeholder_category_counts",
    }:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    count = _validated_count(value["element_count"])
    visible = _validated_count(value["visible_count"])
    if visible > count:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    categories = _validated_fixed_count_map(
        value["placeholder_category_counts"], _PLACEHOLDER_CATEGORIES
    )
    if sum(categories.values()) != count:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    return {
        "element_count": count,
        "visible_count": visible,
        "placeholder_category_counts": categories,
    }


def _validated_visible_text_input(value: object, *, expected_index: int) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _VISIBLE_TEXT_INPUT_KEYS:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    readonly = value["readonly"]
    disabled = value["disabled"]
    enabled = value["enabled"]
    pointer_found = value["pointer_ancestor_found"]
    calendar_present = value["sibling_calendar_icon_present"]
    if not all(
        isinstance(item, bool)
        for item in (readonly, disabled, enabled, pointer_found, calendar_present)
    ):
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    if value["index"] != expected_index or value["type"] != "TEXT" or enabled is disabled:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    value_category = value["value_category"]
    placeholder_category = value["placeholder_category"]
    haspopup_category = value["aria_haspopup_category"]
    if (
        value_category not in _INPUT_VALUE_CATEGORIES
        or placeholder_category not in _PLACEHOLDER_CATEGORIES
        or haspopup_category not in _ARIA_HASPOPUP_CATEGORIES
    ):
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    depth = value["pointer_ancestor_depth"]
    tag = value["pointer_ancestor_tag"]
    pointer_category = value["pointer_ancestor_category"]
    if pointer_found:
        if (
            isinstance(depth, bool)
            or not isinstance(depth, int)
            or depth not in {1, 2, 3, 4}
            or tag not in _CONTROL_TAG_CATEGORIES
            or pointer_category not in _POINTER_ANCESTOR_CATEGORIES
        ):
            raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    elif depth is not None or tag is not None or pointer_category is not None:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    calendar_count = _validated_count(value["sibling_calendar_icon_count"])
    if calendar_present is not (calendar_count > 0):
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    return {
        "index": expected_index,
        "type": "TEXT",
        "readonly": readonly,
        "disabled": disabled,
        "enabled": enabled,
        "value_category": value_category,
        "placeholder_category": placeholder_category,
        "aria_haspopup_category": haspopup_category,
        "pointer_ancestor_found": pointer_found,
        "pointer_ancestor_depth": depth,
        "pointer_ancestor_tag": tag,
        "pointer_ancestor_category": pointer_category,
        "sibling_calendar_icon_present": calendar_present,
        "sibling_calendar_icon_count": calendar_count,
    }


def _validated_product_control_inventory(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "controls",
        "date_markers",
        "date_ranges",
        "inputs",
        "visible_text_inputs",
        "visible_text_input_truncated",
        "scanned_element_count",
        "scan_truncated",
        "input_scan_truncated",
    }:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    if (
        value["scan_truncated"] is not False
        or value["input_scan_truncated"] is not False
        or value["visible_text_input_truncated"] is not False
    ):
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    controls = value["controls"]
    date_markers = value["date_markers"]
    date_ranges = value["date_ranges"]
    inputs = value["inputs"]
    visible_text_inputs = value["visible_text_inputs"]
    if not isinstance(controls, dict) or set(controls) != set(PRODUCT_CONTROL_LABELS):
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    if not isinstance(date_markers, dict) or set(date_markers) != _DATE_SUBJECT_CODES:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    if not isinstance(date_ranges, dict) or set(date_ranges) != _DATE_RANGE_CODES:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    if not isinstance(inputs, dict) or set(inputs) != {"date", "text"}:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    if not isinstance(visible_text_inputs, list) or len(visible_text_inputs) > 20:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    sanitized_inputs = {
        input_type: _validated_input_item(inputs[input_type]) for input_type in ("date", "text")
    }
    if len(visible_text_inputs) != sanitized_inputs["text"]["visible_count"]:
        raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
    sanitized_visible_inputs = [
        _validated_visible_text_input(item, expected_index=index)
        for index, item in enumerate(visible_text_inputs)
    ]
    sanitized_date_markers: dict[str, object] = {}
    for subject in sorted(_DATE_SUBJECT_CODES):
        formats = date_markers[subject]
        if not isinstance(formats, dict) or set(formats) != _DATE_FORMAT_CODES:
            raise SemanticsStopped("PRODUCT_CONTROL_RESULT_INVALID")
        sanitized_date_markers[subject] = {
            format_code: _validated_control_match_pair(formats[format_code])
            for format_code in sorted(_DATE_FORMAT_CODES)
        }
    sanitized_date_ranges = {
        category: _validated_control_match_pair(date_ranges[category])
        for category in sorted(_DATE_RANGE_CODES)
    }
    return {
        "controls": {
            code: _validated_control_item(controls[code]) for code in PRODUCT_CONTROL_LABELS
        },
        "date_markers": sanitized_date_markers,
        "date_ranges": sanitized_date_ranges,
        "inputs": sanitized_inputs,
        "visible_text_inputs": sanitized_visible_inputs,
        "visible_text_input_truncated": False,
        "scanned_element_count": _validated_count(value["scanned_element_count"]),
        "scan_truncated": False,
        "input_scan_truncated": False,
        "input_value_category_observations": len(sanitized_visible_inputs),
        "business_timezone": "Asia/Shanghai",
        "candidate_date_values_output": False,
        "raw_input_values_output": False,
        "raw_text_output": False,
        "class_output": False,
        "title_output": False,
        "placeholder_output": False,
    }


async def _product_control_inventory(
    page: Page, counts: SafetyCounts, *, current_date: date | None = None
) -> dict[str, object]:
    counts.product_control_probe_runs += 1
    effective_current_date = current_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    task = asyncio.create_task(
        page.evaluate(
            _PRODUCT_CONTROL_INVENTORY_PROBE,
            _date_control_candidates(effective_current_date),
        )
    )
    try:
        _, pending = await asyncio.wait({task}, timeout=DOM_EVALUATE_TIMEOUT_SECONDS)
        if pending:
            task.cancel()
            task.add_done_callback(_consume_task_result)
            raise SemanticsStopped("PRODUCT_CONTROL_PROBE_FAILED", counts)
        raw: object = task.result()
    except SemanticsStopped:
        raise
    except Error as exc:
        raise SemanticsStopped("PRODUCT_CONTROL_PROBE_FAILED", counts) from exc
    return _validated_product_control_inventory(raw)


async def _drain_tasks(
    tasks: set[asyncio.Task[None]], *, timeout_seconds: float, counts: SafetyCounts
) -> None:
    snapshot = set(tasks)
    if not snapshot:
        return
    done, pending = await asyncio.wait(snapshot, timeout=timeout_seconds)
    if pending:
        for task in pending:
            task.cancel()
        cancelled_done, still_pending = await asyncio.wait(
            pending, timeout=TASK_CANCEL_GRACE_SECONDS
        )
        for task in done | cancelled_done:
            _consume_task_result(task)
        for task in still_pending:
            task.add_done_callback(_consume_task_result)
        raise SemanticsStopped("CAPTURE_TIMEOUT", counts)
    for task in done:
        _consume_task_result(task)


def _consume_task_result(task: asyncio.Task[object]) -> None:
    if not task.done():
        return
    with suppress(BaseException):
        task.result()


async def _bounded_task_cleanup(tasks: set[asyncio.Task[None]]) -> None:
    snapshot = set(tasks)
    if not snapshot:
        return
    done, pending = await asyncio.wait(snapshot, timeout=TASK_CANCEL_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    cancelled_done, still_pending = await asyncio.wait(pending, timeout=TASK_CANCEL_GRACE_SECONDS)
    for task in done | cancelled_done:
        _consume_task_result(task)
    for task in still_pending:
        task.add_done_callback(_consume_task_result)


async def _bounded_manager_stop(manager: object, counts: SafetyCounts) -> None:
    counts.playwright_stop_attempts += 1
    _checkpoint("PLAYWRIGHT_STOP_BEGIN")
    stop = cast(Any, manager).stop
    task = asyncio.create_task(stop())
    done, pending = await asyncio.wait({task}, timeout=PLAYWRIGHT_STOP_TIMEOUT_SECONDS)
    if pending:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        _checkpoint("PLAYWRIGHT_STOP_TIMEOUT")
        raise SemanticsStopped("PLAYWRIGHT_STOP_FAILED", counts)
    try:
        task.result()
    except Error as exc:
        raise SemanticsStopped("PLAYWRIGHT_STOP_FAILED", counts) from exc
    _checkpoint("PLAYWRIGHT_STOP_DONE")


async def _hard_deadline[ResultT](
    awaitable: Coroutine[Any, Any, ResultT], *, timeout_seconds: float
) -> ResultT:
    task = asyncio.create_task(awaitable)
    done, pending = await asyncio.wait({task}, timeout=timeout_seconds)
    if pending:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        _checkpoint("TOTAL_DEADLINE_EXCEEDED")
        raise SemanticsStopped("CAPTURE_TIMEOUT")
    return task.result()


def _run_with_bounded_shutdown[ResultT](awaitable: Coroutine[Any, Any, ResultT]) -> ResultT:
    _checkpoint("RUNNER_ENTER")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        _checkpoint("RUNNER_SHUTDOWN_BEGIN")
        pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            done, still_pending = loop.run_until_complete(
                asyncio.wait(pending, timeout=TASK_CANCEL_GRACE_SECONDS)
            )
            for task in done:
                _consume_task_result(task)
            for task in still_pending:
                # Closing this private, probe-owned event loop is the final hard
                # boundary.  Suppress only the noisy destruction warning; no task
                # result or exception is hidden from a still-running shared loop.
                cast(Any, task)._log_destroy_pending = False
            if still_pending:
                _checkpoint("RUNNER_PENDING_TASKS_ABANDONED")
        asyncio.set_event_loop(None)
        loop.close()
        _checkpoint("RUNNER_SHUTDOWN_DONE")


async def _tab_control_inventory(
    page: Page, counts: SafetyCounts
) -> tuple[Locator, Locator, dict[str, object]]:
    counts.tab_control_checks += 1
    try:
        traffic = page.locator("a").filter(has_text=re.compile(r"^\s*流量数据\s*$"))
        transaction = page.locator("a").filter(has_text=re.compile(r"^\s*交易数据\s*$"))
        traffic_count = await traffic.count()
        transaction_count = await transaction.count()
        traffic_detail = await _tab_control_detail(traffic, count=traffic_count)
        transaction_detail = await _tab_control_detail(transaction, count=transaction_count)
    except Error as exc:
        raise SemanticsStopped("TAB_CONTROL_CHECK_FAILED", counts) from exc
    return (
        traffic,
        transaction,
        {
            "fixed_tag": "a",
            "exact_text_filter": True,
            "traffic_exact_name_count": traffic_count,
            "transaction_exact_name_count": transaction_count,
            "control_details": {
                "traffic": traffic_detail,
                "transaction": transaction_detail,
            },
        },
    )


def _tab_href_category(value: object) -> TabHrefCategory:
    if value is None:
        return "MISSING"
    if not isinstance(value, str) or len(value) > 2_048 or any(ord(char) < 32 for char in value):
        return "INVALID"
    href = value.strip()
    if not href:
        return "EMPTY"
    if re.fullmatch(r" *javascript:void\(0\);? *", value) is not None:
        return "JAVASCRIPT_VOID_0_EXACT"
    if href.casefold().startswith("javascript:"):
        return "JAVASCRIPT"
    if href.startswith("#"):
        return "HASH_FRAGMENT"
    try:
        resolved = urlsplit(urljoin(TARGET_PAGE_URL, href))
        port = resolved.port
    except ValueError:
        return "INVALID"
    same_origin = (
        resolved.scheme == "https"
        and resolved.hostname == RESPONSE_HOST
        and port in {None, 443}
        and resolved.username is None
        and resolved.password is None
    )
    if not same_origin:
        return "EXTERNAL"
    if resolved.path.rstrip("/") == urlsplit(TARGET_PAGE_URL).path.rstrip("/"):
        return "SAME_ORIGIN_SAME_PATH"
    return "SAME_ORIGIN_OTHER_PATH"


def _tab_same_origin_sanitized_path(value: object, category: TabHrefCategory) -> str | None:
    if category not in {"SAME_ORIGIN_SAME_PATH", "SAME_ORIGIN_OTHER_PATH"}:
        return None
    if not isinstance(value, str):
        return None
    try:
        resolved = urlsplit(urljoin(TARGET_PAGE_URL, value.strip()))
        port = resolved.port
    except ValueError:
        return None
    if (
        resolved.scheme != "https"
        or resolved.hostname != RESPONSE_HOST
        or port not in {None, 443}
        or resolved.username is not None
        or resolved.password is not None
    ):
        return None
    return sanitize_discovery_path(resolved.path or "/")


async def _tab_control_detail(locator: Locator, *, count: object) -> dict[str, object]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise SemanticsStopped("TAB_CONTROL_CHECK_FAILED")
    detail: dict[str, object] = {
        "fixed_tag": "a",
        "exact_text_filter": True,
        "exact_name_count": count,
        "unique_control_inspected": count == 1,
        "visible_count": 0,
        "enabled_count": 0,
        "href_category": None,
        "strict_pdd_origin_verified": False,
        "sanitized_path": None,
        "href_output": False,
        "query_output": False,
        "fragment_output": False,
    }
    if count != 1:
        return detail
    visible = await locator.is_visible()
    enabled = await locator.is_enabled()
    href = await locator.get_attribute("href")
    href_category = _tab_href_category(href)
    detail.update(
        {
            "visible_count": int(visible),
            "enabled_count": int(enabled),
            "href_category": href_category,
            "strict_pdd_origin_verified": href_category
            in {"SAME_ORIGIN_SAME_PATH", "SAME_ORIGIN_OTHER_PATH"},
            "sanitized_path": _tab_same_origin_sanitized_path(href, href_category),
        }
    )
    return detail


async def _require_interactive_tab(
    *,
    detail: object,
    missing_code: ErrorCode,
    counts: SafetyCounts,
    all_details: object,
) -> None:
    safe_detail = {"tab_controls": all_details}
    if not isinstance(detail, dict) or detail.get("exact_name_count") != 1:
        raise SemanticsStopped(missing_code, counts, safe_detail=safe_detail)
    if detail.get("visible_count") != 1 or detail.get("enabled_count") != 1:
        raise SemanticsStopped("TAB_CONTROL_NOT_INTERACTIVE", counts, safe_detail=safe_detail)
    if detail.get("href_category") not in _SAFE_TAB_HREF_CATEGORIES:
        raise SemanticsStopped("TAB_CONTROL_HREF_UNSAFE", counts, safe_detail=safe_detail)


async def _throttle(last_action_at: float, interval_seconds: float) -> None:
    remaining = interval_seconds - (asyncio.get_running_loop().time() - last_action_at)
    if remaining > 0:
        await asyncio.sleep(remaining)


def _traffic_labels_present(dom: dict[str, object]) -> bool:
    current = dom.get("current_dom")
    if not isinstance(current, dict):
        return False
    return any(
        isinstance(current.get(field), dict) and current[field].get("normalized_present") is True
        for field in ("product_visitors", "product_page_views")
    )


async def _capture_semantics(
    page: Page,
    *,
    collection_timeout_ms: int,
    observe_seconds: float,
    counts: SafetyCounts,
    catalog_product_ids: frozenset[str] | None,
) -> tuple[dict[str, dict[str, object]], dict[str, object], float]:
    observations: dict[str, list[dict[str, object]]] = {"list": [], "ready": []}
    tasks: set[asyncio.Task[None]] = set()
    failures: list[SemanticsStopped] = []
    active = True

    async def consume(response: Response, kind: Literal["list", "ready"]) -> None:
        try:
            observations[kind].append(
                await _probe_candidate(response, kind, counts, catalog_product_ids)
            )
        except SemanticsStopped as exc:
            failures.append(exc)
        except Exception as exc:
            failure = SemanticsStopped("SEMANTIC_CAPTURE_FAILED", counts)
            failure.__cause__ = exc
            failures.append(failure)

    def on_response(response: Response) -> None:
        if not active:
            return
        counts.response_events_seen += 1
        kind = _candidate_kind(response)
        if kind is None:
            counts.ignored_response_events += 1
            return
        counts.candidate_response_events += 1
        if kind == "list":
            counts.list_candidate_response_events += 1
        else:
            counts.ready_candidate_response_events += 1
        if counts.candidate_response_events > MAX_CANDIDATE_OBSERVATIONS:
            if not failures:
                failures.append(SemanticsStopped("CANDIDATE_OBSERVATION_LIMIT_EXCEEDED", counts))
            return
        task = asyncio.create_task(consume(response, kind))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    await _assert_safety_gate(page, counts)
    page.on("response", on_response)
    reload_finished_at = 0.0
    try:
        counts.reload_actions += 1
        try:
            await page.reload(wait_until="domcontentloaded", timeout=collection_timeout_ms)
        except Error as exc:
            raise SemanticsStopped("TARGET_RELOAD_FAILED", counts) from exc
        reload_finished_at = asyncio.get_running_loop().time()
        await asyncio.sleep(observe_seconds)
        await _drain_tasks(
            tasks,
            timeout_seconds=collection_timeout_ms / 1_000,
            counts=counts,
        )
        if failures:
            raise failures[0]
        if not _matches_exact_page(page.url, TARGET_PAGE_URL):
            raise SemanticsStopped("TARGET_PAGE_CHANGED", counts)
        await _assert_safety_gate(page, counts)
        dom = await _dom_label_summary(page, counts)
    finally:
        active = False
        page.remove_listener("response", on_response)
        counts.listener_removals += 1
        await _bounded_task_cleanup(tasks)
    return _aggregate(observations), dom, reload_finished_at


async def _capture_list_after_tab_click(
    page: Page,
    locator: Locator,
    *,
    collection_timeout_ms: int,
    observe_seconds: float,
    counts: SafetyCounts,
    catalog_product_ids: frozenset[str] | None,
    restore: bool,
    action_clock: list[float],
) -> tuple[dict[str, object], dict[str, object], float]:
    observations: list[dict[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()
    failures: list[SemanticsStopped] = []
    observed = asyncio.Event()
    active = True

    async def consume(response: Response) -> None:
        try:
            observations.append(
                await _probe_candidate(response, "list", counts, catalog_product_ids)
            )
        except SemanticsStopped as exc:
            failures.append(exc)
        except Exception as exc:
            failure = SemanticsStopped("SEMANTIC_CAPTURE_FAILED", counts)
            failure.__cause__ = exc
            failures.append(failure)
        finally:
            observed.set()

    def on_response(response: Response) -> None:
        if not active:
            return
        counts.response_events_seen += 1
        if _candidate_kind(response) != "list":
            counts.ignored_response_events += 1
            return
        counts.candidate_response_events += 1
        counts.list_candidate_response_events += 1
        if counts.candidate_response_events > MAX_CANDIDATE_OBSERVATIONS:
            failures.append(SemanticsStopped("CANDIDATE_OBSERVATION_LIMIT_EXCEEDED", counts))
            observed.set()
            return
        task = asyncio.create_task(consume(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    await _assert_safety_gate(page, counts)
    page.on("response", on_response)
    action_finished_at = asyncio.get_running_loop().time()
    try:
        if restore:
            counts.traffic_restore_clicks += 1
        else:
            counts.read_only_tab_clicks += 1
        counts.click_actions += 1
        action_clock[0] = asyncio.get_running_loop().time()
        try:
            await locator.click(timeout=collection_timeout_ms)
        except Error as exc:
            code: ErrorCode = (
                "TRAFFIC_TAB_RESTORE_FAILED" if restore else "TRANSACTION_TAB_CLICK_FAILED"
            )
            raise SemanticsStopped(code, counts) from exc
        action_finished_at = action_clock[0]
        try:
            await asyncio.wait_for(observed.wait(), timeout=collection_timeout_ms / 1_000)
        except TimeoutError as exc:
            timeout_code: ErrorCode = (
                "CAPTURE_TIMEOUT"
                if tasks
                else "TRAFFIC_TAB_RESTORE_FAILED"
                if restore
                else "TRANSACTION_TAB_RESPONSE_MISSING"
            )
            raise SemanticsStopped(timeout_code, counts) from exc
        await asyncio.sleep(observe_seconds)
        await _drain_tasks(
            tasks,
            timeout_seconds=collection_timeout_ms / 1_000,
            counts=counts,
        )
        if failures:
            raise failures[0]
        if not _matches_exact_page(page.url, TARGET_PAGE_URL):
            raise SemanticsStopped("TARGET_PAGE_CHANGED", counts)
        await _assert_safety_gate(page, counts)
        dom = await _dom_label_summary(page, counts)
    finally:
        active = False
        page.remove_listener("response", on_response)
        counts.listener_removals += 1
        await _bounded_task_cleanup(tasks)

    signatures = {
        json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        for item in observations
    }
    if not observations:
        raise SemanticsStopped(
            "TRAFFIC_TAB_RESTORE_FAILED" if restore else "TRANSACTION_TAB_RESPONSE_MISSING",
            counts,
        )
    if len(signatures) != 1:
        raise SemanticsStopped("SEMANTIC_OBSERVATION_AMBIGUOUS", counts)
    return {"observation_count": len(observations), **observations[0]}, dom, action_finished_at


async def _tab_switch_semantics(
    page: Page,
    *,
    initial_dom: dict[str, object],
    reload_finished_at: float,
    collection_timeout_ms: int,
    observe_seconds: float,
    counts: SafetyCounts,
    catalog_product_ids: frozenset[str] | None,
    execute: bool,
    min_action_interval_seconds: float,
) -> tuple[dict[str, object], dict[str, object] | None]:
    traffic, transaction, controls = await _tab_control_inventory(page, counts)
    if not execute:
        return {**controls, "switch_executed": False}, None

    control_details = controls.get("control_details")
    if not isinstance(control_details, dict):
        raise SemanticsStopped("TAB_CONTROL_CHECK_FAILED", counts)
    raise SemanticsStopped(
        "TAB_SWITCH_DISABLED_GLOBAL_ROUTE",
        counts,
        safe_detail={"tab_controls": control_details},
    )
    await _require_interactive_tab(
        detail=control_details.get("traffic"),
        missing_code="TRAFFIC_TAB_NOT_UNIQUE",
        counts=counts,
        all_details=control_details,
    )
    await _require_interactive_tab(
        detail=control_details.get("transaction"),
        missing_code="TRANSACTION_TAB_NOT_UNIQUE",
        counts=counts,
        all_details=control_details,
    )
    if not _traffic_labels_present(initial_dom):
        raise SemanticsStopped("INITIAL_TRAFFIC_STATE_UNVERIFIED", counts)

    await _throttle(reload_finished_at, min_action_interval_seconds)
    transaction_result: dict[str, object] | None = None
    failure: SemanticsStopped | None = None
    action_clock = [reload_finished_at]
    restore_status = "NOT_REQUIRED"
    try:
        transaction_list, transaction_dom, _ = await _capture_list_after_tab_click(
            page,
            transaction,
            collection_timeout_ms=collection_timeout_ms,
            observe_seconds=observe_seconds,
            counts=counts,
            catalog_product_ids=catalog_product_ids,
            restore=False,
            action_clock=action_clock,
        )
        transaction_result = {
            "list": transaction_list,
            "dom_labels": transaction_dom,
        }
    except SemanticsStopped as exc:
        failure = exc
    finally:
        if counts.read_only_tab_clicks > 0:
            try:
                if not _matches_exact_page(page.url, TARGET_PAGE_URL):
                    raise SemanticsStopped("TARGET_PAGE_CHANGED", counts)
                await _assert_safety_gate(page, counts)
                await _throttle(action_clock[0], min_action_interval_seconds)
                restored_list, restored_dom, _ = await _capture_list_after_tab_click(
                    page,
                    traffic,
                    collection_timeout_ms=collection_timeout_ms,
                    observe_seconds=observe_seconds,
                    counts=counts,
                    catalog_product_ids=catalog_product_ids,
                    restore=True,
                    action_clock=action_clock,
                )
                if not _traffic_labels_present(restored_dom):
                    raise SemanticsStopped("RESTORE_TRAFFIC_UNVERIFIED", counts)
                restore_status = "SUCCEEDED"
                del restored_list
            except SemanticsStopped as exc:
                restore_status = "FAILED"
                if failure is None:
                    failure = exc
    if failure is not None:
        raise failure
    assert transaction_result is not None
    return (
        {**controls, "switch_executed": True, "restore_status": restore_status},
        transaction_result,
    )


async def semantics_inventory(
    runtime: SemanticsRuntime,
    *,
    observe_seconds: float = OBSERVE_SECONDS,
    catalog_product_ids: frozenset[str] | None = None,
    catalog_match_metadata: dict[str, object] | None = None,
    execute_tab_switch: bool = False,
    min_action_interval_seconds: float = MIN_TAB_ACTION_INTERVAL_SECONDS,
) -> dict[str, object]:
    counts = SafetyCounts()
    _checkpoint("PLAYWRIGHT_START_BEGIN")
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise SemanticsStopped("PLAYWRIGHT_START_FAILED", counts) from exc
    _checkpoint("PLAYWRIGHT_START_DONE")

    stopped: SemanticsStopped | None = None
    result: dict[str, object] | None = None
    try:
        _checkpoint("CDP_CONNECT_BEGIN")
        try:
            browser = await manager.chromium.connect_over_cdp(
                runtime.cdp_endpoint,
                timeout=runtime.connect_timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            raise SemanticsStopped("CDP_CONNECTION_FAILED", counts) from exc
        _checkpoint("CDP_CONNECT_DONE")
        try:
            target, identity_page, context_count, open_page_count, required_page_count = (
                _select_pages(browser)
            )
            _checkpoint("PAGE_GATE_DONE")
            await _verify_identity(identity_page, runtime, counts)
            _checkpoint("IDENTITY_GATE_DONE")
            _checkpoint("BASE_CAPTURE_BEGIN")
            captures, dom, reload_finished_at = await _capture_semantics(
                target,
                collection_timeout_ms=runtime.collection_timeout_ms,
                observe_seconds=observe_seconds,
                counts=counts,
                catalog_product_ids=catalog_product_ids,
            )
            _checkpoint("BASE_CAPTURE_DONE")
            _checkpoint("TAB_CAPTURE_BEGIN")
            tab_controls, transaction = await _tab_switch_semantics(
                target,
                initial_dom=dom,
                reload_finished_at=reload_finished_at,
                collection_timeout_ms=runtime.collection_timeout_ms,
                observe_seconds=observe_seconds,
                counts=counts,
                catalog_product_ids=catalog_product_ids,
                execute=execute_tab_switch,
                min_action_interval_seconds=min_action_interval_seconds,
            )
            _checkpoint("TAB_CAPTURE_DONE")
            result = {
                "status": "PASS",
                "error_code": None,
                "target_path": "/sycm/goods_effect",
                "identity_verified": True,
                "context_count": context_count,
                "open_page_count": open_page_count,
                "target_page_count": 1,
                "required_same_context_page_count": required_page_count,
                "candidate_kind_counts": _candidate_kind_counts(counts),
                "list": captures["list"],
                "ready_date": captures["ready"],
                "dom_labels": dom,
                "tab_controls": tab_controls,
                "transaction_tab": transaction,
            }
            if catalog_match_metadata is not None:
                result["catalog_match"] = catalog_match_metadata
        except SemanticsStopped:
            raise
        except Exception as exc:
            raise SemanticsStopped("SEMANTIC_CAPTURE_FAILED", counts) from exc
    except SemanticsStopped as exc:
        stopped = exc
    finally:
        await _bounded_manager_stop(manager, counts)

    if stopped is not None:
        stopped.counts = counts
        raise stopped
    assert result is not None
    result["safety_counts"] = asdict(counts)
    return result


async def tab_inventory_only(runtime: SemanticsRuntime) -> dict[str, object]:
    """Read only fixed tab-control metadata; never reload, click, or read a response."""
    counts = SafetyCounts()
    _checkpoint("PLAYWRIGHT_START_BEGIN")
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise SemanticsStopped("PLAYWRIGHT_START_FAILED", counts) from exc
    _checkpoint("PLAYWRIGHT_START_DONE")

    stopped: SemanticsStopped | None = None
    result: dict[str, object] | None = None
    try:
        _checkpoint("CDP_CONNECT_BEGIN")
        try:
            browser = await manager.chromium.connect_over_cdp(
                runtime.cdp_endpoint,
                timeout=runtime.connect_timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            raise SemanticsStopped("CDP_CONNECTION_FAILED", counts) from exc
        _checkpoint("CDP_CONNECT_DONE")
        try:
            target, identity_page, context_count, open_page_count, required_page_count = (
                _select_pages(browser)
            )
            _checkpoint("PAGE_GATE_DONE")
            await _verify_identity(identity_page, runtime, counts)
            _checkpoint("IDENTITY_GATE_DONE")
            await _assert_safety_gate(target, counts)
            _checkpoint("TAB_CAPTURE_BEGIN")
            _, _, controls = await _tab_control_inventory(target, counts)
            _checkpoint("TAB_CAPTURE_DONE")
            result = {
                "status": "PASS",
                "error_code": None,
                "mode": "TAB_INVENTORY_ONLY",
                "target_path": "/sycm/goods_effect",
                "identity_verified": True,
                "context_count": context_count,
                "open_page_count": open_page_count,
                "target_page_count": 1,
                "required_same_context_page_count": required_page_count,
                "tab_controls": controls,
                "candidate_kind_counts": _candidate_kind_counts(counts),
            }
        except SemanticsStopped:
            raise
        except Exception as exc:
            raise SemanticsStopped("SEMANTIC_CAPTURE_FAILED", counts) from exc
    except SemanticsStopped as exc:
        stopped = exc
    finally:
        await _bounded_manager_stop(manager, counts)

    if stopped is not None:
        stopped.counts = counts
        raise stopped
    assert result is not None
    result["safety_counts"] = asdict(counts)
    return result


async def product_controls_inventory_only(runtime: SemanticsRuntime) -> dict[str, object]:
    """Inventory fixed product-page control phrases without any browser action."""
    counts = SafetyCounts()
    _checkpoint("PLAYWRIGHT_START_BEGIN")
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise SemanticsStopped("PLAYWRIGHT_START_FAILED", counts) from exc
    _checkpoint("PLAYWRIGHT_START_DONE")

    stopped: SemanticsStopped | None = None
    result: dict[str, object] | None = None
    try:
        _checkpoint("CDP_CONNECT_BEGIN")
        try:
            browser = await manager.chromium.connect_over_cdp(
                runtime.cdp_endpoint,
                timeout=runtime.connect_timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            raise SemanticsStopped("CDP_CONNECTION_FAILED", counts) from exc
        _checkpoint("CDP_CONNECT_DONE")
        try:
            target, identity_page, context_count, open_page_count, required_page_count = (
                _select_pages(browser)
            )
            _checkpoint("PAGE_GATE_DONE")
            await _verify_identity(identity_page, runtime, counts)
            _checkpoint("IDENTITY_GATE_DONE")
            await _assert_safety_gate(target, counts)
            _checkpoint("PRODUCT_CONTROLS_BEGIN")
            inventory = await _product_control_inventory(target, counts)
            _checkpoint("PRODUCT_CONTROLS_DONE")
            result = {
                "status": "PASS",
                "error_code": None,
                "mode": "PRODUCT_CONTROLS_INVENTORY_ONLY",
                "target_path": "/sycm/goods_effect",
                "identity_verified": True,
                "context_count": context_count,
                "open_page_count": open_page_count,
                "target_page_count": 1,
                "required_same_context_page_count": required_page_count,
                "product_controls": inventory,
                "candidate_kind_counts": _candidate_kind_counts(counts),
            }
        except SemanticsStopped:
            raise
        except Exception as exc:
            raise SemanticsStopped("SEMANTIC_CAPTURE_FAILED", counts) from exc
    except SemanticsStopped as exc:
        stopped = exc
    finally:
        await _bounded_manager_stop(manager, counts)

    if stopped is not None:
        stopped.counts = counts
        raise stopped
    assert result is not None
    result["safety_counts"] = asdict(counts)
    return result


def _error_payload(stopped: SemanticsStopped) -> dict[str, object]:
    return {
        "status": "STOPPED",
        "error_code": stopped.code,
        "safe_failure_detail": stopped.safe_detail,
        "candidate_kind_counts": _candidate_kind_counts(stopped.counts),
        "list": None,
        "ready_date": None,
        "dom_labels": None,
        "safety_counts": asdict(stopped.counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read strictly redacted product-business semantics from one fixed page."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--confirm-read-only", action="store_true")
    parser.add_argument("--confirm-tab-switch", action="store_true")
    parser.add_argument("--inventory-tabs-only", action="store_true")
    parser.add_argument("--inventory-product-controls-only", action="store_true")
    parser.add_argument("--match-latest-product-catalog", action="store_true")
    args = parser.parse_args()
    try:
        if not args.confirm_read_only:
            raise SemanticsStopped("READ_ONLY_CONFIRMATION_REQUIRED")
        _checkpoint("CLI_ARGS_VALIDATED")
        if args.confirm_tab_switch:
            raise SemanticsStopped("TAB_SWITCH_DISABLED_GLOBAL_ROUTE")
        if args.inventory_tabs_only and args.inventory_product_controls_only:
            raise SemanticsStopped("INCOMPATIBLE_MODE_FLAGS")
        if args.match_latest_product_catalog and (
            args.inventory_tabs_only or args.inventory_product_controls_only
        ):
            raise SemanticsStopped("INCOMPATIBLE_MODE_FLAGS")
        runtime = _load_runtime(args.config, args.connection_id)
        _checkpoint("CLI_RUNTIME_LOADED")
        catalog_product_ids: frozenset[str] | None = None
        catalog_match_metadata: dict[str, object] | None = None
        if args.match_latest_product_catalog:
            _checkpoint("CATALOG_LOAD_BEGIN")
            catalog_product_ids, catalog_match_metadata = _load_latest_product_catalog(runtime)
            _checkpoint("CATALOG_LOAD_DONE")
        operation = (
            tab_inventory_only(runtime)
            if args.inventory_tabs_only
            else product_controls_inventory_only(runtime)
            if args.inventory_product_controls_only
            else semantics_inventory(
                runtime,
                execute_tab_switch=args.confirm_tab_switch,
                catalog_product_ids=catalog_product_ids,
                catalog_match_metadata=catalog_match_metadata,
            )
        )
        payload = _run_with_bounded_shutdown(
            _hard_deadline(
                operation,
                timeout_seconds=(
                    CLI_TAB_INVENTORY_DEADLINE_SECONDS
                    if args.inventory_tabs_only
                    else CLI_PRODUCT_CONTROLS_DEADLINE_SECONDS
                    if args.inventory_product_controls_only
                    else CLI_TAB_DEADLINE_SECONDS
                    if args.confirm_tab_switch
                    else CLI_BASE_DEADLINE_SECONDS
                ),
            )
        )
        _checkpoint("CLI_RESULT_READY")
    except SemanticsStopped as exc:
        _checkpoint("CLI_STOPPED")
        payload = _error_payload(exc)
        exit_code = 1
    except Exception:
        _checkpoint("CLI_STOPPED")
        payload = _error_payload(SemanticsStopped("UNEXPECTED_FAILURE"))
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
