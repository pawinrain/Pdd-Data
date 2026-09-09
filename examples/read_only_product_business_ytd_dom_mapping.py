from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from playwright.async_api import Error, Page, async_playwright

if TYPE_CHECKING or __package__:
    from examples import read_only_product_business_semantics as semantics_module
else:  # Direct execution makes the examples directory sys.path[0].
    import read_only_product_business_semantics as semantics_module

IDENTITY_PAGE_URL = semantics_module.IDENTITY_PAGE_URL
REQUIRED_SAME_CONTEXT_PAGES = semantics_module.REQUIRED_SAME_CONTEXT_PAGES
TARGET_PAGE_URL = semantics_module.TARGET_PAGE_URL
SafetyCounts = semantics_module.SafetyCounts
SemanticsRuntime = semantics_module.SemanticsRuntime
SemanticsStopped = semantics_module.SemanticsStopped
_assert_safety_gate = semantics_module._assert_safety_gate
_load_runtime = semantics_module._load_runtime
_matches_exact_page = semantics_module._matches_exact_page
_select_pages = semantics_module._select_pages
_verify_identity = semantics_module._verify_identity

BUSINESS_LABEL_ALIASES: dict[str, dict[str, str]] = {
    "paying_buyer_count": {
        "PAYING_BUYER_COUNT": "支付买家数",
        "PAYING_BUYER": "支付买家",
    },
    "paid_order_count": {
        "PAID_ORDER_COUNT": "支付订单数",
        "PAID_ORDER": "支付订单",
    },
    "paid_goods_quantity": {
        "PAID_QUANTITY": "支付件数",
        "PAID_PRODUCT_QUANTITY": "支付商品件数",
    },
    "paid_amount": {
        "PAID_AMOUNT_YUAN_ASCII": "支付金额(元)",
        "PAID_AMOUNT_YUAN_FULLWIDTH": "支付金额\uff08元\uff09",
        "PAID_AMOUNT_NO_UNIT": "支付金额",
    },
    "goods_visitor_count": {
        "PRODUCT_VISITOR_COUNT": "商品访客数",
        "VISITOR": "访客",
    },
    "goods_page_view_count": {
        "PRODUCT_PAGE_VIEW_COUNT": "商品浏览量",
        "PAGE_VIEW": "浏览量",
    },
}
TIME_LABELS = {"today": "今日", "yesterday": "昨日"}
COUNTEREVIDENCE_PHRASES = {
    "YESTERDAY_SAME_PERIOD": "昨日同期",
    "YESTERDAY_CUTOFF": "昨日截至",
    "SAME_TIME_YESTERDAY": "与昨日同一时段",
    "AS_OF_YESTERDAY": "截至昨日",
    "DATA_UPDATING": "数据更新中",
    "DATA_DELAYED": "数据延迟",
}

_BUSINESS_FIELDS = frozenset(BUSINESS_LABEL_ALIASES)
_ALIAS_CODES = frozenset(alias for aliases in BUSINESS_LABEL_ALIASES.values() for alias in aliases)
_TAG_CATEGORIES = frozenset(
    {
        "TABLE",
        "THEAD",
        "TBODY",
        "TR",
        "TH",
        "TD",
        "DIV",
        "SPAN",
        "LI",
        "SECTION",
        "ARTICLE",
        "OTHER",
    }
)
_ROLE_CATEGORIES = frozenset(
    {
        "NONE",
        "TABLE",
        "GRID",
        "ROW",
        "COLUMNHEADER",
        "CELL",
        "GRIDCELL",
        "GROUP",
        "TABPANEL",
        "OTHER",
    }
)
_SPAN_CATEGORIES = frozenset({"MISSING", "ONE", "TWO", "THREE_TO_SIX", "MORE_THAN_SIX", "INVALID"})
_ALIAS_STATUSES = frozenset({"NONE", "UNIQUE", "AMBIGUOUS"})
_ANCHOR_STATUSES = frozenset({"STRUCTURAL_CANDIDATE", "UNVERIFIED"})

_DOM_MAPPING_PROBE = r"""
payload => {
    const normalize = value => String(value)
        .normalize('NFKC')
        .replace(/[\s\u200b\ufeff]+/gu, '')
        .replace(/[\uFF1A:]+$/u, '');
    const visible = element => {
        const style = getComputedStyle(element);
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && element.getClientRects().length > 0;
    };
    const tagCategory = element => {
        const tag = String(element.tagName || '').toUpperCase();
        return new Set([
            'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'DIV', 'SPAN', 'LI',
            'SECTION', 'ARTICLE'
        ]).has(tag) ? tag : 'OTHER';
    };
    const roleCategory = element => {
        const role = String(element.getAttribute('role') || '').trim().toUpperCase();
        return new Set([
            'TABLE', 'GRID', 'ROW', 'COLUMNHEADER', 'CELL', 'GRIDCELL', 'GROUP',
            'TABPANEL'
        ]).has(role) ? role : role ? 'OTHER' : 'NONE';
    };
    const spanCategory = (element, name) => {
        const raw = element.getAttribute(name);
        if (raw === null) return 'MISSING';
        if (!/^[1-9]\d*$/.test(raw)) return 'INVALID';
        const value = Number(raw);
        if (value === 1) return 'ONE';
        if (value === 2) return 'TWO';
        if (value <= 6) return 'THREE_TO_SIX';
        return 'MORE_THAN_SIX';
    };
    const all = Array.from(document.querySelectorAll('body *'));
    const scanTruncated = all.length > 50000;
    const elements = all.slice(0, 50000);
    const exactVisible = label => elements.filter(element => {
        if (!visible(element)) return false;
        const text = typeof element.textContent === 'string' ? element.textContent : '';
        return normalize(text) === normalize(label);
    });
    const location = element => ({
        in_table: element.closest('table,[role="table"],[role="grid"]') !== null,
        in_thead: element.closest('thead') !== null,
        in_columnheader: element.closest('th,[role="columnheader"]') !== null,
    });
    const relation = element => {
        let ancestor = element.parentElement;
        for (let depth = 1; depth <= 6 && ancestor !== null; depth += 1) {
            const descendants = Array.from(ancestor.querySelectorAll('*')).filter(visible);
            const todayCount = descendants.filter(candidate => {
                const text = typeof candidate.textContent === 'string' ? candidate.textContent : '';
                return normalize(text) === normalize(payload.time_labels.today);
            }).length;
            const yesterdayCount = descendants.filter(candidate => {
                const text = typeof candidate.textContent === 'string' ? candidate.textContent : '';
                return normalize(text) === normalize(payload.time_labels.yesterday);
            }).length;
            if (todayCount > 0 || yesterdayCount > 0) {
                return {
                    found: true,
                    depth,
                    today_exact_count: todayCount,
                    yesterday_exact_count: yesterdayCount,
                    tag_category: tagCategory(ancestor),
                    role_category: roleCategory(ancestor),
                    colspan_category: spanCategory(ancestor, 'colspan'),
                    rowspan_category: spanCategory(ancestor, 'rowspan'),
                };
            }
            ancestor = ancestor.parentElement;
        }
        return {
            found: false,
            depth: null,
            today_exact_count: 0,
            yesterday_exact_count: 0,
            tag_category: null,
            role_category: null,
            colspan_category: null,
            rowspan_category: null,
        };
    };
    const fields = {};
    for (const [fieldCode, aliases] of Object.entries(payload.business_aliases)) {
        const aliasCounts = {};
        const observations = [];
        let inTable = 0;
        let inThead = 0;
        let inColumnheader = 0;
        for (const [aliasCode, label] of Object.entries(aliases)) {
            const matches = exactVisible(label);
            aliasCounts[aliasCode] = matches.length;
            for (const element of matches.slice(0, 20)) {
                const where = location(element);
                if (where.in_table) inTable += 1;
                if (where.in_thead) inThead += 1;
                if (where.in_columnheader) inColumnheader += 1;
                observations.push({alias_code: aliasCode, ...relation(element)});
            }
        }
        const matched = Object.entries(aliasCounts)
            .filter(([, count]) => count > 0)
            .map(([code]) => code);
        const visibleCount = Object.values(aliasCounts).reduce((total, count) => total + count, 0);
        fields[fieldCode] = {
            alias_visible_exact_counts: aliasCounts,
            visible_exact_element_count: visibleCount,
            alias_match_status: matched.length === 0
                ? 'NONE'
                : matched.length === 1 ? 'UNIQUE' : 'AMBIGUOUS',
            matched_alias_code: matched.length === 1 ? matched[0] : null,
            in_table_count: inTable,
            in_thead_count: inThead,
            in_columnheader_count: inColumnheader,
            container_observations: observations,
            container_observations_truncated: visibleCount > observations.length,
        };
    }
    const phraseCounts = {};
    for (const [code, phrase] of Object.entries(payload.counterevidence_phrases)) {
        let count = 0;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node !== null) {
            const parent = node.parentElement;
            if (
                parent !== null
                && visible(parent)
                && normalize(node.nodeValue || '').includes(normalize(phrase))
            ) {
                count += 1;
            }
            node = walker.nextNode();
        }
        phraseCounts[code] = count;
    }
    return {
        scanned_element_count: elements.length,
        scan_truncated: scanTruncated,
        structure_counts: {
            visible_table_count: elements.filter(
                element => element.tagName === 'TABLE' && visible(element)
            ).length,
            visible_grid_count: elements.filter(element => {
                const role = String(element.getAttribute('role') || '').toLowerCase();
                return ['table', 'grid'].includes(role) && visible(element);
            }).length,
            visible_thead_count: elements.filter(
                element => element.tagName === 'THEAD' && visible(element)
            ).length,
            visible_columnheader_count: elements.filter(element => {
                const role = String(element.getAttribute('role') || '').toLowerCase();
                return (element.tagName === 'TH' || role === 'columnheader') && visible(element);
            }).length,
        },
        time_label_counts: {
            today_visible_exact_count: exactVisible(payload.time_labels.today).length,
            yesterday_visible_exact_count: exactVisible(payload.time_labels.yesterday).length,
        },
        fields,
        counterevidence_phrase_counts: phraseCounts,
    };
}
"""


class MappingStopped(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    return value


def _fixed_count_map(value: object, expected: frozenset[str]) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != expected:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    return {key: _count(value[key]) for key in sorted(expected)}


def _nullable_category(value: object, allowed: frozenset[str], *, found: bool) -> str | None:
    if found:
        if not isinstance(value, str) or value not in allowed:
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        return value
    if value is not None:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    return None


def _container_observation(value: object, aliases: frozenset[str]) -> dict[str, object]:
    expected = {
        "alias_code",
        "found",
        "depth",
        "today_exact_count",
        "yesterday_exact_count",
        "tag_category",
        "role_category",
        "colspan_category",
        "rowspan_category",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    found = value["found"]
    if not isinstance(found, bool) or value["alias_code"] not in aliases:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    depth = value["depth"]
    if found:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth not in range(1, 7):
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    elif depth is not None:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    today = _count(value["today_exact_count"])
    yesterday = _count(value["yesterday_exact_count"])
    if found is not (today > 0 or yesterday > 0):
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    return {
        "alias_code": value["alias_code"],
        "found": found,
        "depth": depth,
        "today_exact_count": today,
        "yesterday_exact_count": yesterday,
        "tag_category": _nullable_category(value["tag_category"], _TAG_CATEGORIES, found=found),
        "role_category": _nullable_category(value["role_category"], _ROLE_CATEGORIES, found=found),
        "colspan_category": _nullable_category(
            value["colspan_category"], _SPAN_CATEGORIES, found=found
        ),
        "rowspan_category": _nullable_category(
            value["rowspan_category"], _SPAN_CATEGORIES, found=found
        ),
    }


def _validated_result(value: object) -> dict[str, object]:
    expected = {
        "scanned_element_count",
        "scan_truncated",
        "structure_counts",
        "time_label_counts",
        "fields",
        "counterevidence_phrase_counts",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["scan_truncated"] is not False
    ):
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    structure = _fixed_count_map(
        value["structure_counts"],
        frozenset(
            {
                "visible_table_count",
                "visible_grid_count",
                "visible_thead_count",
                "visible_columnheader_count",
            }
        ),
    )
    time_counts = _fixed_count_map(
        value["time_label_counts"],
        frozenset({"today_visible_exact_count", "yesterday_visible_exact_count"}),
    )
    counterevidence = _fixed_count_map(
        value["counterevidence_phrase_counts"], frozenset(COUNTEREVIDENCE_PHRASES)
    )
    fields = value["fields"]
    if not isinstance(fields, dict) or set(fields) != _BUSINESS_FIELDS:
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    sanitized_fields: dict[str, object] = {}
    structural_candidates = 0
    for field in sorted(_BUSINESS_FIELDS):
        item = fields[field]
        expected_item = {
            "alias_visible_exact_counts",
            "visible_exact_element_count",
            "alias_match_status",
            "matched_alias_code",
            "in_table_count",
            "in_thead_count",
            "in_columnheader_count",
            "container_observations",
            "container_observations_truncated",
        }
        if not isinstance(item, dict) or set(item) != expected_item:
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        aliases = frozenset(BUSINESS_LABEL_ALIASES[field])
        alias_counts = _fixed_count_map(item["alias_visible_exact_counts"], aliases)
        visible_count = _count(item["visible_exact_element_count"])
        if visible_count != sum(alias_counts.values()):
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        status = item["alias_match_status"]
        matched = item["matched_alias_code"]
        present_aliases = [code for code, count in alias_counts.items() if count > 0]
        expected_status = (
            "NONE"
            if not present_aliases
            else "UNIQUE"
            if len(present_aliases) == 1
            else "AMBIGUOUS"
        )
        if status not in _ALIAS_STATUSES or status != expected_status:
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        if (status == "UNIQUE" and matched != present_aliases[0]) or (
            status != "UNIQUE" and matched is not None
        ):
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        locations = {
            key: _count(item[key])
            for key in ("in_table_count", "in_thead_count", "in_columnheader_count")
        }
        if any(count > visible_count for count in locations.values()):
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        observations = item["container_observations"]
        truncated = item["container_observations_truncated"]
        if (
            not isinstance(observations, list)
            or len(observations) > 60
            or not isinstance(truncated, bool)
        ):
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        sanitized_observations = [
            _container_observation(observation, aliases) for observation in observations
        ]
        if truncated is not (visible_count > len(observations)):
            raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
        has_yesterday_container = any(
            observation["found"] and cast(int, observation["yesterday_exact_count"]) > 0
            for observation in sanitized_observations
        )
        if status == "UNIQUE" and has_yesterday_container:
            structural_candidates += 1
        sanitized_fields[field] = {
            "alias_visible_exact_counts": alias_counts,
            "visible_exact_element_count": visible_count,
            "alias_match_status": status,
            "matched_alias_code": matched,
            **locations,
            "container_observations": sanitized_observations,
            "container_observations_truncated": truncated,
            "yesterday_container_present": has_yesterday_container,
        }
    anchor_status = (
        "STRUCTURAL_CANDIDATE"
        if structural_candidates == len(_BUSINESS_FIELDS)
        and time_counts["yesterday_visible_exact_count"] > 0
        and sum(counterevidence.values()) == 0
        else "UNVERIFIED"
    )
    if anchor_status not in _ANCHOR_STATUSES:  # pragma: no cover - closed construction
        raise MappingStopped("DOM_MAPPING_RESULT_INVALID")
    return {
        "scanned_element_count": _count(value["scanned_element_count"]),
        "scan_truncated": False,
        "structure_counts": structure,
        "time_label_counts": time_counts,
        "fields": sanitized_fields,
        "counterevidence_phrase_counts": counterevidence,
        "ytd_anchor_status": anchor_status,
        "raw_text_output": False,
        "business_value_output": False,
        "product_id_output": False,
        "product_name_output": False,
        "class_output": False,
        "title_output": False,
        "query_output": False,
    }


async def _safety_gate_all_pages(target: Page, identity_page: Page, counts: SafetyCounts) -> None:
    pages = [target, identity_page]
    for required_url in REQUIRED_SAME_CONTEXT_PAGES:
        matches = [
            page
            for page in target.context.pages
            if not page.is_closed() and _matches_exact_page(page.url, required_url)
        ]
        if len(matches) != 1:
            raise MappingStopped("REQUIRED_CONTEXT_PAGE_CHANGED")
        pages.append(matches[0])
    for page in pages:
        await _assert_safety_gate(page, counts)


async def inspect_dom(runtime: SemanticsRuntime) -> dict[str, object]:
    counts = SafetyCounts()
    try:
        manager = await async_playwright().start()
    except Error as exc:
        raise MappingStopped("PLAYWRIGHT_START_FAILED") from exc
    result: dict[str, object] | None = None
    stopped: Exception | None = None
    try:
        try:
            browser = await manager.chromium.connect_over_cdp(
                runtime.cdp_endpoint,
                timeout=runtime.connect_timeout_ms,
                is_local=True,
                no_defaults=True,
            )
        except Error as exc:
            raise MappingStopped("CDP_CONNECTION_FAILED") from exc
        target, identity_page, context_count, open_page_count, required_page_count = _select_pages(
            browser
        )
        await _safety_gate_all_pages(target, identity_page, counts)
        await _verify_identity(identity_page, runtime, counts)
        try:
            raw: object = await target.evaluate(
                _DOM_MAPPING_PROBE,
                {
                    "business_aliases": BUSINESS_LABEL_ALIASES,
                    "time_labels": TIME_LABELS,
                    "counterevidence_phrases": COUNTEREVIDENCE_PHRASES,
                },
            )
        except Error as exc:
            raise MappingStopped("DOM_MAPPING_READ_FAILED") from exc
        result = {
            "status": "PASS",
            "error_code": None,
            "target_path": "/sycm/goods_effect",
            "identity_verified": True,
            "context_count": context_count,
            "open_page_count": open_page_count,
            "required_same_context_page_count": required_page_count,
            **_validated_result(raw),
        }
    except Exception as exc:
        stopped = exc
    finally:
        counts.playwright_stop_attempts += 1
        try:
            await manager.stop()
        except Error as exc:
            if stopped is None:
                stopped = MappingStopped("PLAYWRIGHT_STOP_FAILED")
                stopped.__cause__ = exc
    if stopped is not None:
        raise stopped
    assert result is not None
    result["safety_counts"] = asdict(counts)
    return result


def _error_payload(code: str) -> dict[str, object]:
    return {
        "status": "STOPPED",
        "error_code": code,
        "fields": None,
        "counterevidence_phrase_counts": None,
        "safety_claim": {
            "reload_actions": 0,
            "navigation_actions": 0,
            "click_actions": 0,
            "response_body_reads": 0,
            "browser_close_actions": 0,
            "page_close_actions": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zero-action DOM structure inventory for product-business Ytd mapping."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    try:
        if not args.confirm_read_only:
            raise MappingStopped("READ_ONLY_CONFIRMATION_REQUIRED")
        runtime = _load_runtime(args.config, args.connection_id)
        payload = asyncio.run(inspect_dom(runtime))
    except (MappingStopped, SemanticsStopped) as exc:
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
