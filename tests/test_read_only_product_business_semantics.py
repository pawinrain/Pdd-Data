from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast

import pytest

from examples import read_only_product_business_semantics as semantics_module
from examples.read_only_product_business_semantics import (
    COMPARISON_RISK_PHRASES,
    DOM_LABELS,
    IDENTITY_PAGE_URL,
    LIST_RESPONSE_PATH,
    MAX_RESPONSE_BYTES,
    PRODUCT_CONTROL_LABELS,
    READY_DATE_RESPONSE_PATH,
    REQUIRED_SAME_CONTEXT_PAGES,
    TARGET_PAGE_URL,
    SafetyCounts,
    SemanticsRuntime,
    SemanticsStopped,
    _aggregate,
    _bounded_manager_stop,
    _candidate_kind,
    _capture_semantics,
    _date_control_candidates,
    _dom_label_summary,
    _error_payload,
    _hard_deadline,
    _id_summary,
    _lexical_flags,
    _list_response_summary,
    _load_latest_product_catalog,
    _metric_format_category,
    _metric_summaries,
    _probe_candidate,
    _product_control_inventory,
    _read_latest_active_product_catalog,
    _ready_response_summary,
    _request_summary,
    _require_interactive_tab,
    _run_with_bounded_shutdown,
    _strict_json,
    _tab_control_detail,
    _tab_control_inventory,
    _tab_href_category,
    _timestamp_summary,
    _validated_dom_group,
    _validated_product_control_inventory,
    _verify_identity,
    main,
    product_controls_inventory_only,
    semantics_inventory,
    tab_inventory_only,
)
from pdd_data_mcp.contracts.models import DatasetType
from pdd_data_mcp.errors import LockBusyError, SnapshotCorruptError


def list_request(*, query_type: int = 0) -> dict[str, object]:
    return {
        "actVs": 0,
        "crawlerInfo": "private-crawler-state",
        "endDate": "2026-09-07",
        "pageNum": 1,
        "pageSize": 10,
        "queryType": query_type,
        "sortCol": 0,
        "sortType": 0,
        "startDate": "2026-09-07",
    }


def list_payload(*, metric_value: object = "0") -> dict[str, object]:
    return {
        "success": True,
        "errorCode": 0,
        "errorMsg": None,
        "result": {
            "delayData": 0,
            "goodsDetailList": [
                {
                    "goodsId": 123456789,
                    "goodsName": "private product name",
                    "goodsStatus": 1,
                    "statDate": "2026-09-07",
                    "payOrdrUsrCnt": metric_value,
                    "payOrdrCnt": "2",
                    "payOrdrGoodsQty": "3",
                    "payOrdrAmt": "12.34",
                    "goodsUv": None,
                    "goodsPv": "not-numeric",
                    "payOrdrUsrCntYtd": "private-ytd-buyers",
                    "payOrdrCntYtd": "private-ytd-orders",
                    "payOrdrGoodsQtyYtd": "private-ytd-quantity",
                    "payOrdrAmtYtd": "private-ytd-amount",
                    "goodsUvYtd": "private-ytd-visitors",
                    "goodsPvYtd": "private-ytd-page-views",
                }
            ],
            "totalNum": 1,
            "timestamp": 1_788_796_800_000,
        },
    }


def ready_payload() -> dict[str, object]:
    return {
        "success": True,
        "errorCode": 0,
        "errorMsg": None,
        "result": "2026-09-07",
    }


def dom_payload(*, traffic: bool) -> dict[str, object]:
    def group(labels: dict[str, str]) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for code in labels:
            visible = traffic and code in {"product_visitors", "product_page_views"}
            visible = visible or (not traffic and code.startswith("paid_"))
            count = 1 if visible else 0
            result[code] = {
                "exact_count": count,
                "normalized_count": count,
                "normalized_contains_count": count,
            }
        return result

    return {
        "current": group(DOM_LABELS),
        "risk_phrases": group(COMPARISON_RISK_PHRASES),
    }


def product_control_payload() -> dict[str, object]:
    def empty_control() -> dict[str, object]:
        return {
            "exact_normalized_element_count": 0,
            "visible_element_count": 0,
            "direct_interactive_count": 0,
            "direct_interactive_tag_counts": {},
            "direct_interactive_role_counts": {},
            "direct_interactive_category_counts": {},
            "nearest_interactive_ancestor_count": 0,
            "ancestor_depth_counts": {},
            "ancestor_tag_counts": {},
            "ancestor_role_counts": {},
            "ancestor_category_counts": {},
        }

    def match_pair() -> dict[str, object]:
        return {"exact": empty_control(), "contains": empty_control()}

    controls = {code: empty_control() for code in PRODUCT_CONTROL_LABELS}
    controls["yesterday"] = {
        "exact_normalized_element_count": 2,
        "visible_element_count": 1,
        "direct_interactive_count": 1,
        "direct_interactive_tag_counts": {"A": 1},
        "direct_interactive_role_counts": {"NONE": 1},
        "direct_interactive_category_counts": {"NATIVE_LINK": 1},
        "nearest_interactive_ancestor_count": 1,
        "ancestor_depth_counts": {"2": 1},
        "ancestor_tag_counts": {"BUTTON": 1},
        "ancestor_role_counts": {"NONE": 1},
        "ancestor_category_counts": {"NATIVE_BUTTON": 1},
    }
    return {
        "controls": controls,
        "date_markers": {
            subject: {
                format_code: match_pair()
                for format_code in (
                    "YYYY_MM_DD_HYPHEN",
                    "YYYY_MM_DD_SLASH",
                    "YYYY_MM_DD_DOT",
                    "MM_DD",
                    "M_MONTH_D_DAY",
                )
            }
            for subject in ("CURRENT_DATE", "PREVIOUS_DATE")
        },
        "date_ranges": {
            category: match_pair() for category in ("CURRENT_TO_CURRENT", "PREVIOUS_TO_PREVIOUS")
        },
        "inputs": {
            "date": {
                "element_count": 2,
                "visible_count": 1,
                "placeholder_category_counts": {"DATE_START": 1, "DATE_END": 1},
            },
            "text": {
                "element_count": 1,
                "visible_count": 1,
                "placeholder_category_counts": {"OTHER": 1},
            },
        },
        "visible_text_inputs": [
            {
                "index": 0,
                "type": "TEXT",
                "readonly": True,
                "disabled": False,
                "enabled": True,
                "value_category": "ISO_DATE_RANGE",
                "placeholder_category": "OTHER",
                "aria_haspopup_category": "MISSING",
                "pointer_ancestor_found": True,
                "pointer_ancestor_depth": 2,
                "pointer_ancestor_tag": "DIV",
                "pointer_ancestor_category": "CURSOR_POINTER_ONLY",
                "sibling_calendar_icon_present": True,
                "sibling_calendar_icon_count": 1,
            }
        ],
        "visible_text_input_truncated": False,
        "scanned_element_count": 100,
        "scan_truncated": False,
        "input_scan_truncated": False,
    }


class FakeLocator:
    def __init__(
        self,
        *,
        count: int = 0,
        text: str | None = None,
        visible: bool = True,
        enabled: bool = True,
        href: str | None = None,
        click: Any = None,
    ) -> None:
        self._count = count
        self._text = text
        self._visible = visible
        self._enabled = enabled
        self._href = href
        self._click = click

    async def count(self) -> int:
        return self._count

    async def text_content(self) -> str | None:
        return self._text

    async def is_visible(self) -> bool:
        return self._visible

    async def is_enabled(self) -> bool:
        return self._enabled

    async def get_attribute(self, name: str) -> str | None:
        assert name == "href"
        return self._href

    async def click(self, **kwargs: object) -> None:
        assert kwargs == {"timeout": 9000}
        if self._click is not None:
            await self._click()


class FakeAnchorRoot:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def filter(self, *, has_text: re.Pattern[str]) -> FakeLocator:
        if "流量数据" in has_text.pattern:
            return self.page.traffic_locator
        if "交易数据" in has_text.pattern:
            return self.page.transaction_locator
        return FakeLocator()


class FakeRequest:
    def __init__(self, payload: object, *, method: str = "POST") -> None:
        self._payload = payload
        self.method = method
        self.post_data_json_reads = 0

    @property
    def post_data_json(self) -> object:
        self.post_data_json_reads += 1
        return self._payload


class FakeResponse:
    def __init__(
        self,
        path: str,
        *,
        request_payload: object,
        response_payload: object,
        host: str = "mms.pinduoduo.com",
        method: str = "POST",
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        content_length: str | None = None,
    ) -> None:
        self.url = f"https://{host}{path}?private=query#private-fragment"
        self.request = FakeRequest(request_payload, method=method)
        self.status = status
        self.headers = {
            "content-type": content_type,
            "authorization": "must-not-output",
        }
        self._body = json.dumps(response_payload, separators=(",", ":")).encode()
        if content_length is not None:
            self.headers["content-length"] = content_length
        self.body_reads = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        return self._body


class HangingResponse(FakeResponse):
    async def body(self) -> bytes:
        self.body_reads += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class CancellationResistantResponse(FakeResponse):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cancellations = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                if self.cancellations >= 2:
                    raise


def list_response(*, query_type: int = 0) -> FakeResponse:
    return FakeResponse(
        LIST_RESPONSE_PATH,
        request_payload=list_request(query_type=query_type),
        response_payload=list_payload(),
    )


def ready_response() -> FakeResponse:
    return FakeResponse(
        READY_DATE_RESPONSE_PATH,
        request_payload={"crawlerInfo": "private-crawler-state"},
        response_payload=ready_payload(),
    )


class FakePage:
    def __init__(
        self,
        url: str,
        *,
        closed: bool = False,
        identity_text: str | None = None,
        reload_responses: list[FakeResponse] | None = None,
        transaction_responses: list[FakeResponse] | None = None,
        restore_responses: list[FakeResponse] | None = None,
    ) -> None:
        self.url = url
        self._closed = closed
        self.identity_text = identity_text
        self.reload_responses = reload_responses or []
        self.transaction_responses = transaction_responses or []
        self.restore_responses = restore_responses or []
        self.listeners: list[Any] = []
        self.reload_calls = 0
        self.current_tab = "traffic"
        self.context: FakeContext | None = None
        self.traffic_locator = FakeLocator(
            count=1,
            href="#traffic",
            click=self._click_traffic,
        )
        self.transaction_locator = FakeLocator(
            count=1,
            href="#transaction",
            click=self._click_transaction,
        )

    def is_closed(self) -> bool:
        return self._closed

    def locator(self, selector: str) -> Any:
        if selector == "html":
            return FakeLocator(count=1, text=self.identity_text)
        if selector == "a":
            return FakeAnchorRoot(self)
        return FakeLocator(count=0)

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def _emit(self, responses: list[FakeResponse]) -> None:
        for response in responses:
            for callback in list(self.listeners):
                callback(response)
        await asyncio.sleep(0)

    async def reload(self, **kwargs: object) -> None:
        assert kwargs.get("wait_until") == "domcontentloaded"
        assert isinstance(kwargs.get("timeout"), int)
        self.reload_calls += 1
        self.current_tab = "traffic"
        await self._emit(self.reload_responses)

    async def _click_transaction(self) -> None:
        self.current_tab = "transaction"
        await self._emit(self.transaction_responses)

    async def _click_traffic(self) -> None:
        self.current_tab = "traffic"
        await self._emit(self.restore_responses)

    async def evaluate(self, script: str, labels: object) -> dict[str, object]:
        assert "scrollLeft" not in script
        assert "requestAnimationFrame" not in script
        if isinstance(labels, dict) and labels.get("labels") == PRODUCT_CONTROL_LABELS:
            assert set(labels) == {"labels", "date_candidates", "range_candidates"}
            assert "parentElement" in script
            return product_control_payload()
        assert labels == {
            "metric_labels": DOM_LABELS,
            "risk_phrases": COMPARISON_RISK_PHRASES,
        }
        return dom_payload(traffic=self.current_tab == "traffic")


class NeverResolvingEvaluatePage(FakePage):
    def __init__(self) -> None:
        super().__init__(TARGET_PAGE_URL)
        self.cancellations = 0

    async def evaluate(self, script: str, labels: object) -> dict[str, object]:
        assert "scrollLeft" not in script
        assert "requestAnimationFrame" not in script
        assert labels == {
            "metric_labels": DOM_LABELS,
            "risk_phrases": COMPARISON_RISK_PHRASES,
        }
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellations += 1


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        for page in pages:
            page.context = self


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    async def connect_over_cdp(self, endpoint: str, **kwargs: object) -> FakeBrowser:
        assert endpoint == "http://127.0.0.1:9222"
        assert kwargs == {"timeout": 1234, "is_local": True, "no_defaults": True}
        return self.browser


class FakeManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class CancellationResistantStopManager:
    def __init__(self) -> None:
        self.cancellations = 0

    async def stop(self) -> None:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                if self.cancellations >= 2:
                    raise


class NeverStoppingManager:
    def __init__(self) -> None:
        self.cancellations = 0

    async def stop(self) -> None:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellations += 1


class FakePlaywrightStarter:
    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager

    async def start(self) -> FakeManager:
        return self.manager


def runtime(store_id: str = "123456789") -> SemanticsRuntime:
    return SemanticsRuntime(
        cdp_endpoint="http://127.0.0.1:9222",
        connect_timeout_ms=1234,
        collection_timeout_ms=9000,
        expected_store_id="",
        expected_store_id_sha256=hashlib.sha256(store_id.encode()).hexdigest(),
    )


def runtime_with_catalog(config: Any) -> SemanticsRuntime:
    base = runtime()
    return SemanticsRuntime(
        cdp_endpoint=base.cdp_endpoint,
        connect_timeout_ms=base.connect_timeout_ms,
        collection_timeout_ms=base.collection_timeout_ms,
        expected_store_id=base.expected_store_id,
        expected_store_id_sha256=base.expected_store_id_sha256,
        catalog_storage=config.storage,
        catalog_allowed_store_ids=frozenset({"st_current_01"}),
        catalog_store_id="st_current_01",
        catalog_max_response_bytes=262_144,
    )


def catalog_summary(
    *,
    store_id: str = "st_current_01",
    effective_status: str = "ACTIVE",
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_id="s_0123456789abcdef0123456789abcdef",
        store_id=store_id,
        dataset_type=DatasetType.PRODUCT_CATALOG,
        effective_status=effective_status,
        quality_status="VALID",
        source="PDD_BROWSER_CDP",
        coverage=SimpleNamespace(value="COMPLETE"),
        record_count=2,
    )


def catalog_read(
    *,
    records: list[dict[str, object]],
    next_cursor: str | None = None,
    store_id: str = "st_current_01",
) -> SimpleNamespace:
    snapshot_id = "s_0123456789abcdef0123456789abcdef"
    return SimpleNamespace(
        snapshot_id=snapshot_id,
        manifest={
            "snapshot_id": snapshot_id,
            "store_id": store_id,
            "dataset_type": "product_catalog",
            "source": "PDD_BROWSER_CDP",
            "record_count": 2,
        },
        effective_status="ACTIVE",
        invalidation=None,
        data=None,
        records=records,
        next_cursor=next_cursor,
    )


class FakeCatalogRepository:
    def __init__(
        self,
        *,
        items: list[SimpleNamespace],
        reads: dict[str | None, SimpleNamespace] | None = None,
    ) -> None:
        self.items = items
        self.reads = reads or {}
        self.list_calls = 0
        self.read_calls: list[str | None] = []

    def list_snapshots(self, **kwargs: object) -> SimpleNamespace:
        assert kwargs["store_id"] == "st_current_01"
        assert kwargs["dataset_type"] is DatasetType.PRODUCT_CATALOG
        assert kwargs["captured_from"] is None
        assert kwargs["captured_to"] is None
        assert kwargs["limit"] == 100
        self.list_calls += 1
        return SimpleNamespace(items=self.items, next_cursor=None)

    def read_snapshot(self, **kwargs: object) -> SimpleNamespace:
        assert kwargs["snapshot_id"] == "s_0123456789abcdef0123456789abcdef"
        assert kwargs["page_size"] == 200
        cursor = cast(str | None, kwargs["cursor"])
        self.read_calls.append(cursor)
        return self.reads[cursor]


def run_hanging_cli_subprocess(mode: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    python_paths = [str(root), str(root / "src")]
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    program = f"""
import asyncio
import sys
from examples import read_only_product_business_semantics as module

module.CLI_BASE_DEADLINE_SECONDS = 0.2 if {mode!r} == "manager" else 0.02
module.TASK_CANCEL_GRACE_SECONDS = 0.02
module.PLAYWRIGHT_STOP_TIMEOUT_SECONDS = 0.02
module._load_runtime = lambda *_args: object()

class NeverStoppingManager:
    async def stop(self):
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

async def never_finishes(_runtime, **_kwargs):
    if {mode!r} == "manager":
        await module._bounded_manager_stop(NeverStoppingManager(), module.SafetyCounts())
        return {{}}
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

module.semantics_inventory = never_finishes
sys.argv = [
    "read_only_product_business_semantics.py",
    "--config", "unused.toml",
    "--connection-id", "offline_test_connection",
    "--confirm-read-only",
]
raise SystemExit(module.main())
"""
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )


def browser_with_required_pages(target: FakePage, identity: FakePage) -> FakeBrowser:
    return FakeBrowser(
        [
            FakeContext(
                [
                    target,
                    identity,
                    *(FakePage(url) for url in REQUIRED_SAME_CONTEXT_PAGES),
                ]
            )
        ]
    )


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"value":1,"value":2}', "DUPLICATE_JSON_KEY"),
        (b'{"value":NaN}', "NON_FINITE_JSON_NUMBER"),
        (b"{not-json}", "INVALID_JSON_RESPONSE"),
    ],
)
def test_strict_json_rejects_ambiguous_or_invalid_values(raw: bytes, code: str) -> None:
    with pytest.raises(SemanticsStopped, match=code):
        _strict_json(raw)


def test_only_two_exact_response_contracts_match() -> None:
    listed = list_response()
    ready = ready_response()
    assert _candidate_kind(cast(Any, listed)) == "list"
    assert _candidate_kind(cast(Any, ready)) == "ready"
    rejected = [
        FakeResponse(
            LIST_RESPONSE_PATH,
            request_payload=list_request(),
            response_payload=list_payload(),
            host="notpinduoduo.com",
        ),
        FakeResponse(
            LIST_RESPONSE_PATH,
            request_payload=list_request(),
            response_payload=list_payload(),
            method="GET",
        ),
        FakeResponse(
            "/sydney/api/goodsDataShow/unknown",
            request_payload={},
            response_payload={"secret": True},
        ),
    ]
    assert all(_candidate_kind(cast(Any, item)) is None for item in rejected)
    assert all(item.request.post_data_json_reads == 0 for item in rejected)
    assert all(item.body_reads == 0 for item in rejected)


def test_request_summary_emits_only_approved_fields() -> None:
    summary = _request_summary(list_request(), kind="list")
    assert summary == {
        "startDate": "2026-09-07",
        "endDate": "2026-09-07",
        "pageNum": 1,
        "pageSize": 10,
        "queryType": 0,
        "sortCol": 0,
        "sortType": 0,
        "actVs": 0,
    }
    assert "crawler" not in json.dumps(summary).casefold()
    with pytest.raises(SemanticsStopped, match="REQUEST_CONTRACT_MISMATCH"):
        _request_summary({**list_request(), "extra": 1}, kind="list")


def test_response_summary_redacts_ids_names_and_metric_values() -> None:
    request = _request_summary(list_request(), kind="list")
    result = _list_response_summary(
        list_payload(),
        request=request,
        catalog_product_ids=frozenset({"123456789", "999"}),
    )
    serialized = json.dumps(result)
    for secret in (
        "123456789",
        "private product name",
        "12.34",
        "not-numeric",
        "private-ytd-buyers",
        "private-ytd-amount",
    ):
        assert secret not in serialized
    assert result["coverage_counts"] == {
        "total": 1,
        "captured": 1,
        "statDate_unique_count": 1,
        "statDate_parseable_count": 1,
        "all_statDates_within_request": True,
    }
    ids = cast(dict[str, object], result["product_ids"])
    assert ids["unique_count"] == 1
    assert ids["d2_match_count"] == 1
    fields = cast(dict[str, dict[str, object]], result["metric_fields"])
    assert fields["payOrdrUsrCnt"]["zero_count"] == 1
    assert fields["goodsUv"]["null_count"] == 1
    assert fields["goodsPv"]["parseable_count"] == 0
    assert fields["goodsPv"]["format_category_counts"] == {"SUFFIXED_OR_OTHER": 1}
    ytd = cast(dict[str, object], result["ytd_suffix_fields"])
    assert ytd["interpretation"] == "UNVERIFIED_COMPARISON_SUFFIX_ONLY"
    assert ytd["previous_natural_day_semantics_verified"] is False
    assert ytd["ready_date_linkage_verified"] is False
    assert ytd["source_finalized_inferred"] is False
    ytd_fields = cast(dict[str, dict[str, object]], ytd["fields"])
    assert ytd_fields["payOrdrUsrCntYtd"]["type_counts"] == {"string": 1}
    assert ytd_fields["payOrdrUsrCntYtd"]["format_category_counts"] == {"SUFFIXED_OR_OTHER": 1}
    pairing = cast(dict[str, object], result["current_ytd_pairing"])
    assert pairing["fully_paired_row_count"] == 1
    assert pairing["incomplete_pair_row_count"] == 0
    assert pairing["scalar_values_compared"] is False


def test_ytd_suffix_summary_is_format_only_and_does_not_claim_yesterday() -> None:
    payload = list_payload()
    result_payload = cast(dict[str, object], payload["result"])
    rows = cast(list[dict[str, object]], result_payload["goodsDetailList"])
    rows[0]["payOrdrUsrCntYtd"] = "<987654321"
    rows[0]["payOrdrAmtYtd"] = "￥98,765,432.10"

    summary = _list_response_summary(
        payload,
        request=_request_summary(list_request(), kind="list"),
        catalog_product_ids=None,
    )

    serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    assert "987654321" not in serialized
    assert "98,765,432.10" not in serialized
    ytd = cast(dict[str, object], summary["ytd_suffix_fields"])
    fields = cast(dict[str, dict[str, object]], ytd["fields"])
    assert fields["payOrdrUsrCntYtd"]["format_category_counts"] == {"LESS_THAN_NUMERIC": 1}
    assert fields["payOrdrAmtYtd"]["format_category_counts"] == {"CNY_SYMBOL_PREFIX": 1}
    signature = cast(dict[str, object], fields["payOrdrAmtYtd"]["lexical_signature"])
    assert signature["unique_value_count"] == 1
    assert signature["scalar_values_output"] is False
    assert signature["value_hashes_output"] is False
    assert signature["numeric_bounds_output"] is False
    feature_counts = cast(dict[str, int], signature["feature_true_counts"])
    assert feature_counts["has_digit"] == 1
    assert feature_counts["has_currency"] == 1
    assert feature_counts["has_comma"] == 1
    assert feature_counts["has_private_use_codepoint"] == 0
    assert ytd["previous_natural_day_semantics_verified"] is False
    assert "yesterday" not in serialized.casefold()


def test_row_contract_accepts_required_subset_and_ignores_extra_values() -> None:
    payload = list_payload()
    result_payload = cast(dict[str, object], payload["result"])
    rows = cast(list[dict[str, object]], result_payload["goodsDetailList"])
    rows[0]["privateUnknownKey"] = "private-unknown-value"

    summary = _list_response_summary(
        payload,
        request=_request_summary(list_request(), kind="list"),
        catalog_product_ids=None,
    )

    serialized = json.dumps(summary, sort_keys=True)
    assert "privateUnknownKey" not in serialized
    assert "private-unknown-value" not in serialized


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (None, "NULL"),
        (12, "NON_STRING"),
        ("  ", "BLANK"),
        ("\u2014", "DASH_PLACEHOLDER"),
        ("12.50", "PLAIN_NUMERIC"),
        ("1,234.50", "GROUPED_NUMERIC"),
        ("1-10", "NUMERIC_RANGE"),
        ("1,000至2,000", "NUMERIC_RANGE"),
        ("1~10人", "RANGE_WITH_UNIT"),
        ("1,000\uff5e2,000元", "RANGE_WITH_UNIT"),
        ("12.5\uff05", "PERCENT"),
        ("<10", "LESS_THAN_NUMERIC"),
        (">1,000", "GREATER_THAN_NUMERIC"),
        ("12人", "COUNT_UNIT_SUFFIX"),
        ("1,234次", "COUNT_UNIT_SUFFIX"),
        ("12.50元", "YUAN_SUFFIX"),
        ("¥12.50", "CNY_SYMBOL_PREFIX"),
        ("￥1,234", "CNY_SYMBOL_PREFIX"),
        ("N/A", "PLACEHOLDER_OTHER"),
        ("private-suffix", "SUFFIXED_OR_OTHER"),
    ],
)
def test_metric_format_category_is_fixed_and_value_free(value: object, category: str) -> None:
    assert _metric_format_category(value) == category


def test_lexical_flags_detect_fixed_character_classes_without_returning_values() -> None:
    encoded_like = "deadBEE1"
    flags = _lexical_flags(encoded_like)
    assert flags["has_digit"] is True
    assert flags["has_ascii_letter"] is True
    assert flags["ascii_hexlike"] is True
    assert flags["base64like"] is True

    unusual = "\ue123\U0001f600\ufffd"
    unusual_flags = _lexical_flags(unusual)
    assert unusual_flags["has_private_use_codepoint"] is True
    assert unusual_flags["has_non_bmp"] is True
    assert unusual_flags["has_replacement_character"] is True
    assert unusual not in json.dumps(unusual_flags, ensure_ascii=False)


def test_row_contract_failure_detail_has_only_counts_types_and_unknown_key_hashes() -> None:
    payload = list_payload()
    result = cast(dict[str, object], payload["result"])
    rows = cast(list[dict[str, object]], result["goodsDetailList"])
    row = rows[0]
    row.pop("goodsPv")
    row["privateUnknownKey"] = "private-unknown-value"
    request = _request_summary(list_request(), kind="list")

    with pytest.raises(SemanticsStopped, match="ROW_CONTRACT_MISMATCH") as caught:
        _list_response_summary(payload, request=request, catalog_product_ids=None)

    detail = caught.value.safe_detail
    assert isinstance(detail, dict)
    serialized = json.dumps(detail, sort_keys=True)
    for forbidden in (
        "privateUnknownKey",
        "private-unknown-value",
        "123456789",
        "private product name",
        "12.34",
    ):
        assert forbidden not in serialized
    expected_hash = hashlib.sha256(b"pdd-product-row-key:privateUnknownKey").hexdigest()
    assert detail["unknown_key_name_sha256"] == [expected_hash]
    assert detail["missing_known_key_occurrences"] == 1
    assert detail["extra_unknown_key_occurrences"] == 1
    diagnostics = cast(dict[str, dict[str, object]], detail["known_field_diagnostics"])
    assert diagnostics["goodsPv"]["missing_count"] == 1
    assert diagnostics["payOrdrAmt"]["type_counts"] == {"string": 1}
    assert diagnostics["payOrdrAmt"]["parseable_count"] == 1
    assert _error_payload(caught.value)["safe_failure_detail"] == detail


def test_ready_date_and_timestamp_are_bounded_semantics() -> None:
    assert _ready_response_summary(ready_payload()) == {"readyDate": "2026-09-07"}
    timestamp = _timestamp_summary(1_788_796_800_000)
    assert timestamp["epoch_unit"] == "MILLISECONDS"
    assert cast(str, timestamp["iso_utc"]).endswith("Z")
    assert timestamp["interpretation"] == "OBSERVED_RESULT_TIMESTAMP_ONLY"
    assert "updated" not in json.dumps(timestamp).casefold()


def test_metric_and_id_helpers_only_return_counts() -> None:
    row = cast(
        dict[str, object], cast(dict[str, object], list_payload()["result"])["goodsDetailList"][0]
    )
    metrics = _metric_summaries([row])
    ids = _id_summary([row], None)
    assert metrics["payOrdrAmt"]["parseable_count"] == 1
    assert ids["d2_match_evaluated"] is False
    assert ids["d2_match_count"] is None
    assert "goodsId" not in json.dumps(ids)


def test_latest_active_catalog_reads_only_id_field_and_returns_safe_metadata() -> None:
    repository = FakeCatalogRepository(
        items=[
            catalog_summary(effective_status="SEMANTICALLY_INVALIDATED"),
            catalog_summary(),
        ],
        reads={
            None: catalog_read(
                records=[
                    {
                        "platform_product_id": "123456789",
                        "name": "private product name one",
                        "price_cents": 999999,
                    },
                    {
                        "platform_product_id": "987654321",
                        "name": "private product name two",
                        "price_cents": 888888,
                    },
                ]
            )
        },
    )

    product_ids, metadata = _read_latest_active_product_catalog(
        cast(Any, repository), store_id="st_current_01"
    )

    assert product_ids == frozenset({"123456789", "987654321"})
    assert metadata["manifest_record_count"] == 2
    assert metadata["records_read_count"] == 2
    assert metadata["unique_platform_product_id_count"] == 2
    assert metadata["latest_active_selected"] is True
    assert metadata["platform_product_id_values_output"] is False
    assert metadata["product_name_fields_accessed"] is False
    serialized = json.dumps(metadata, sort_keys=True)
    for secret in (
        "123456789",
        "987654321",
        "private product name one",
        "private product name two",
        "999999",
        "888888",
        "s_0123456789abcdef0123456789abcdef",
        "st_current_01",
    ):
        assert secret not in serialized


def test_latest_catalog_missing_and_cross_store_are_hard_failures() -> None:
    missing = FakeCatalogRepository(items=[])
    with pytest.raises(SemanticsStopped, match="CATALOG_SNAPSHOT_NOT_FOUND"):
        _read_latest_active_product_catalog(cast(Any, missing), store_id="st_current_01")

    wrong_store = FakeCatalogRepository(items=[catalog_summary(store_id="st_other_01")])
    with pytest.raises(SemanticsStopped, match="CATALOG_STORE_MISMATCH"):
        _read_latest_active_product_catalog(cast(Any, wrong_store), store_id="st_current_01")
    assert wrong_store.read_calls == []


def test_catalog_loader_maps_corruption_and_lock_failure_to_fixed_codes(
    monkeypatch: pytest.MonkeyPatch, config: Any
) -> None:
    class BrokenRepository:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        @contextmanager
        def service_lock(self, *, timeout: float) -> Any:
            assert timeout == 0
            if isinstance(self.failure, LockBusyError):
                raise self.failure
            yield

        def initialize(self) -> None:
            if not isinstance(self.failure, LockBusyError):
                raise self.failure

    for failure, code in (
        (SnapshotCorruptError("private corrupt path"), "CATALOG_STORAGE_INVALID"),
        (LockBusyError("private lock owner"), "CATALOG_LOCK_BUSY"),
    ):
        broken = BrokenRepository(failure)
        monkeypatch.setattr(
            semantics_module,
            "LocalFileSnapshotRepository",
            lambda *_args, _broken=broken, **_kwargs: _broken,
        )
        with pytest.raises(SemanticsStopped, match=code) as caught:
            _load_latest_product_catalog(runtime_with_catalog(config))
        serialized = json.dumps(_error_payload(caught.value), sort_keys=True)
        assert "private corrupt path" not in serialized
        assert "private lock owner" not in serialized


def test_content_length_gate_precedes_request_and_body_reads() -> None:
    response = list_response()
    response.headers["content-length"] = str(MAX_RESPONSE_BYTES + 1)
    with pytest.raises(SemanticsStopped, match="RESPONSE_TOO_LARGE"):
        asyncio.run(_probe_candidate(cast(Any, response), "list", SafetyCounts(), None))
    assert response.request.post_data_json_reads == 0
    assert response.body_reads == 0


def test_dom_group_allows_only_counts_and_booleans() -> None:
    raw = cast(dict[str, object], dom_payload(traffic=True)["current"])
    result = _validated_dom_group(raw)
    assert result["product_visitors"]["exact_present"] is True
    assert result["paid_amount"]["normalized_contains_count"] == 0
    assert all(label not in json.dumps(result, ensure_ascii=False) for label in DOM_LABELS.values())


def test_dom_summary_adds_only_fixed_risk_phrase_codes_and_counts() -> None:
    page = FakePage(TARGET_PAGE_URL)
    result = asyncio.run(_dom_label_summary(cast(Any, page), SafetyCounts()))
    risk_counts = cast(dict[str, dict[str, int]], result["comparison_risk_phrase_counts"])
    assert set(risk_counts) == set(COMPARISON_RISK_PHRASES)
    assert all(
        set(counts) == {"exact_count", "normalized_count", "normalized_contains_count"}
        for counts in risk_counts.values()
    )
    assert result["comparison_risk_phrase_text_output"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert all(phrase not in serialized for phrase in COMPARISON_RISK_PHRASES.values())


def test_dom_evaluate_timeout_is_process_bounded_without_scroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantics_module, "DOM_EVALUATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(semantics_module, "TASK_CANCEL_GRACE_SECONDS", 0.01)
    page = NeverResolvingEvaluatePage()
    counts = SafetyCounts()
    started_at = monotonic()

    with pytest.raises(SemanticsStopped, match="DOM_LABEL_PROBE_FAILED"):
        _run_with_bounded_shutdown(_dom_label_summary(cast(Any, page), counts))

    assert monotonic() - started_at < 0.5
    assert page.cancellations >= 1
    assert counts.temporary_scroll_probe_runs == 0


def test_identity_gate_matches_hash_without_returning_identity() -> None:
    page = FakePage(IDENTITY_PAGE_URL, identity_text="店铺ID:123456789")
    counts = SafetyCounts()
    assert asyncio.run(_verify_identity(cast(Any, page), runtime(), counts)) is None
    assert counts.identity_dom_reads == 1

    with pytest.raises(SemanticsStopped, match="IDENTITY_MISMATCH"):
        asyncio.run(_verify_identity(cast(Any, page), runtime("999999999"), counts))


def test_capture_reloads_once_and_never_reads_unknown_response() -> None:
    unknown = FakeResponse(
        "/unknown",
        request_payload={"private": True},
        response_payload={"secret": True},
    )
    listed = list_response()
    ready = ready_response()
    target = FakePage(
        TARGET_PAGE_URL,
        reload_responses=[unknown, listed, ready],
    )
    counts = SafetyCounts()
    captures, dom, _ = asyncio.run(
        _capture_semantics(
            cast(Any, target),
            collection_timeout_ms=9000,
            observe_seconds=0,
            counts=counts,
            catalog_product_ids=frozenset({"123456789"}),
        )
    )
    assert target.reload_calls == 1
    assert target.listeners == []
    assert captures["list"]["observation_count"] == 1
    assert captures["ready"]["status"] == "OBSERVED"
    assert captures["ready"]["readyDate"] == "2026-09-07"
    assert dom["temporary_horizontal_scroll_executed"] is False
    assert counts.temporary_scroll_probe_runs == 0
    assert unknown.request.post_data_json_reads == 0
    assert unknown.body_reads == 0
    assert counts.response_body_reads == 2
    assert counts.list_candidate_response_events == 1
    assert counts.ready_candidate_response_events == 1


def test_capture_keeps_list_when_ready_date_is_not_observed() -> None:
    target = FakePage(TARGET_PAGE_URL, reload_responses=[list_response()])
    counts = SafetyCounts()

    captures, _, _ = asyncio.run(
        _capture_semantics(
            cast(Any, target),
            collection_timeout_ms=9000,
            observe_seconds=0,
            counts=counts,
            catalog_product_ids=None,
        )
    )

    assert captures["list"]["observation_count"] == 1
    assert captures["ready"] == {
        "status": "NOT_OBSERVED_THIS_RELOAD",
        "observation_count": 0,
        "request": None,
        "readyDate": None,
    }
    assert counts.list_candidate_response_events == 1
    assert counts.ready_candidate_response_events == 0


def test_list_observation_is_required_and_must_be_unique() -> None:
    with pytest.raises(SemanticsStopped, match="REQUIRED_RESPONSE_MISSING"):
        _aggregate({"list": [], "ready": []})
    with pytest.raises(SemanticsStopped, match="SEMANTIC_OBSERVATION_AMBIGUOUS"):
        _aggregate({"list": [{"same": True}, {"same": True}], "ready": []})


def test_error_payload_reports_only_candidate_kind_counts() -> None:
    counts = SafetyCounts(
        candidate_response_events=3,
        list_candidate_response_events=2,
        ready_candidate_response_events=1,
    )
    payload = _error_payload(SemanticsStopped("REQUIRED_RESPONSE_MISSING", counts))
    assert payload["candidate_kind_counts"] == {"list": 2, "ready": 1}


def test_capture_cancels_a_body_read_that_never_finishes() -> None:
    hanging = HangingResponse(
        LIST_RESPONSE_PATH,
        request_payload=list_request(),
        response_payload=list_payload(),
    )
    target = FakePage(
        TARGET_PAGE_URL,
        reload_responses=[hanging, ready_response()],
    )
    counts = SafetyCounts()

    with pytest.raises(SemanticsStopped, match="CAPTURE_TIMEOUT"):
        asyncio.run(
            _capture_semantics(
                cast(Any, target),
                collection_timeout_ms=10,
                observe_seconds=0,
                counts=counts,
                catalog_product_ids=None,
            )
        )

    assert target.listeners == []
    assert hanging.body_reads == 1
    assert counts.listener_removals == 1


def test_capture_timeout_does_not_wait_forever_for_cancellation_resistant_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantics_module, "TASK_CANCEL_GRACE_SECONDS", 0.01)
    hanging = CancellationResistantResponse(
        LIST_RESPONSE_PATH,
        request_payload=list_request(),
        response_payload=list_payload(),
    )
    target = FakePage(
        TARGET_PAGE_URL,
        reload_responses=[hanging, ready_response()],
    )
    with pytest.raises(SemanticsStopped, match="CAPTURE_TIMEOUT"):
        asyncio.run(
            _capture_semantics(
                cast(Any, target),
                collection_timeout_ms=10,
                observe_seconds=0,
                counts=SafetyCounts(),
                catalog_product_ids=None,
            )
        )
    assert hanging.cancellations >= 1


def test_manager_stop_has_a_hard_timeout_even_if_initial_cancel_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantics_module, "PLAYWRIGHT_STOP_TIMEOUT_SECONDS", 0.01)
    manager = CancellationResistantStopManager()
    counts = SafetyCounts()
    with pytest.raises(SemanticsStopped, match="PLAYWRIGHT_STOP_FAILED"):
        asyncio.run(_bounded_manager_stop(cast(Any, manager), counts))
    assert counts.playwright_stop_attempts == 1
    assert manager.cancellations >= 1


def test_private_runner_closes_without_waiting_for_never_stopping_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantics_module, "PLAYWRIGHT_STOP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(semantics_module, "TASK_CANCEL_GRACE_SECONDS", 0.01)
    manager = NeverStoppingManager()
    counts = SafetyCounts()
    started_at = monotonic()

    with pytest.raises(SemanticsStopped, match="PLAYWRIGHT_STOP_FAILED"):
        _run_with_bounded_shutdown(_bounded_manager_stop(cast(Any, manager), counts))

    assert monotonic() - started_at < 0.5
    assert counts.playwright_stop_attempts == 1
    assert manager.cancellations >= 1


def test_private_runner_enforces_total_deadline_on_cancellation_resistant_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantics_module, "TASK_CANCEL_GRACE_SECONDS", 0.01)
    cancellations = 0

    async def never_finishes() -> None:
        nonlocal cancellations
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations += 1

    started_at = monotonic()
    with pytest.raises(SemanticsStopped, match="CAPTURE_TIMEOUT"):
        _run_with_bounded_shutdown(_hard_deadline(never_finishes(), timeout_seconds=0.01))

    assert monotonic() - started_at < 0.5
    assert cancellations >= 1


@pytest.mark.parametrize(
    ("mode", "expected_code", "checkpoint"),
    [
        ("deadline", "CAPTURE_TIMEOUT", "TOTAL_DEADLINE_EXCEEDED"),
        ("manager", "PLAYWRIGHT_STOP_FAILED", "PLAYWRIGHT_STOP_TIMEOUT"),
    ],
)
def test_cli_subprocess_exits_when_cancellation_is_ignored(
    mode: str, expected_code: str, checkpoint: str
) -> None:
    completed = run_hanging_cli_subprocess(mode)

    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error_code"] == expected_code
    diagnostic_lines = completed.stderr.splitlines()
    assert f"PDD_PRODUCT_BUSINESS_SEMANTICS:{checkpoint}" in diagnostic_lines
    assert "PDD_PRODUCT_BUSINESS_SEMANTICS:RUNNER_SHUTDOWN_DONE" in diagnostic_lines
    assert "PDD_PRODUCT_BUSINESS_SEMANTICS:CLI_STOPPED" in diagnostic_lines
    assert all(
        re.fullmatch(r"PDD_PRODUCT_BUSINESS_SEMANTICS:[A-Z_]+", line) for line in diagnostic_lines
    )


def test_tab_controls_lock_unique_anchor_exact_text_and_safe_href() -> None:
    page = FakePage(TARGET_PAGE_URL)
    counts = SafetyCounts()
    traffic, transaction, summary = asyncio.run(_tab_control_inventory(cast(Any, page), counts))
    assert traffic is page.traffic_locator
    assert transaction is page.transaction_locator
    assert summary["fixed_tag"] == "a"
    assert summary["exact_text_filter"] is True
    assert summary["traffic_exact_name_count"] == 1
    assert summary["transaction_exact_name_count"] == 1
    details = cast(dict[str, dict[str, object]], summary["control_details"])
    assert details["traffic"]["href_category"] == "HASH_FRAGMENT"
    assert details["transaction"]["href_category"] == "HASH_FRAGMENT"
    assert all(item["href_output"] is False for item in details.values())


def test_tab_inventory_only_has_zero_reload_click_and_response_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(TARGET_PAGE_URL)
    identity = FakePage(IDENTITY_PAGE_URL, identity_text="店铺ID:123456789")
    manager = FakeManager(browser_with_required_pages(target, identity))
    monkeypatch.setattr(
        semantics_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(tab_inventory_only(runtime()))

    assert result["status"] == "PASS"
    assert result["mode"] == "TAB_INVENTORY_ONLY"
    assert result["candidate_kind_counts"] == {"list": 0, "ready": 0}
    safety = cast(dict[str, int], result["safety_counts"])
    assert safety["reload_actions"] == 0
    assert safety["click_actions"] == 0
    assert safety["response_events_seen"] == 0
    assert safety["request_json_reads"] == 0
    assert safety["response_body_reads"] == 0
    assert target.reload_calls == 0
    assert manager.stop_calls == 1


def test_product_controls_inventory_only_has_zero_browser_actions_or_response_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(TARGET_PAGE_URL)
    identity = FakePage(IDENTITY_PAGE_URL, identity_text="店铺ID:123456789")
    manager = FakeManager(browser_with_required_pages(target, identity))
    monkeypatch.setattr(
        semantics_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    result = asyncio.run(product_controls_inventory_only(runtime()))

    assert result["status"] == "PASS"
    assert result["mode"] == "PRODUCT_CONTROLS_INVENTORY_ONLY"
    assert result["candidate_kind_counts"] == {"list": 0, "ready": 0}
    safety = cast(dict[str, int], result["safety_counts"])
    assert safety["reload_actions"] == 0
    assert safety["click_actions"] == 0
    assert safety["response_events_seen"] == 0
    assert safety["request_json_reads"] == 0
    assert safety["response_body_reads"] == 0
    assert safety["product_control_probe_runs"] == 1
    assert target.reload_calls == 0
    assert manager.stop_calls == 1
    controls = cast(dict[str, object], result["product_controls"])
    assert controls["input_value_category_observations"] == 1
    assert controls["raw_input_values_output"] is False
    assert controls["raw_text_output"] is False
    assert controls["class_output"] is False
    assert controls["title_output"] is False
    assert controls["candidate_date_values_output"] is False
    assert controls["business_timezone"] == "Asia/Shanghai"
    serialized = json.dumps(result, ensure_ascii=False)
    assert all(label not in serialized for label in PRODUCT_CONTROL_LABELS.values())


def test_date_control_candidates_are_generated_locally_but_never_returned() -> None:
    payload = _date_control_candidates(date(2026, 9, 8))
    candidates = cast(dict[str, dict[str, str]], payload["date_candidates"])
    assert candidates["CURRENT_DATE"]["YYYY_MM_DD_HYPHEN"] == "2026-09-08"
    assert candidates["PREVIOUS_DATE"]["YYYY_MM_DD_HYPHEN"] == "2026-09-07"
    assert "9月8日" in candidates["CURRENT_DATE"].values()

    sanitized = _validated_product_control_inventory(product_control_payload())
    serialized = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    assert "2026-09-08" not in serialized
    assert "2026-09-07" not in serialized
    assert "9月8日" not in serialized
    assert sanitized["candidate_date_values_output"] is False
    assert set(cast(dict[str, object], sanitized["date_markers"])) == {
        "CURRENT_DATE",
        "PREVIOUS_DATE",
    }


def test_product_control_probe_passes_fixed_date_candidates_without_returning_values() -> None:
    class CapturePage:
        argument: object = None

        async def evaluate(self, script: str, argument: object) -> dict[str, object]:
            assert "dateCandidates" in script
            self.argument = argument
            return product_control_payload()

    page = CapturePage()
    result = asyncio.run(
        _product_control_inventory(cast(Any, page), SafetyCounts(), current_date=date(2026, 9, 8))
    )

    argument = cast(dict[str, object], page.argument)
    assert set(argument) == {"labels", "date_candidates", "range_candidates"}
    serialized_result = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert "2026-09-08" not in serialized_result
    assert "2026-09-07" not in serialized_result
    assert result["raw_text_output"] is False


def test_product_control_validator_rejects_unapproved_date_format_without_echo() -> None:
    raw = product_control_payload()
    date_markers = cast(dict[str, dict[str, object]], raw["date_markers"])
    current = date_markers["CURRENT_DATE"]
    current["PRIVATE_DATE_FORMAT"] = current["YYYY_MM_DD_HYPHEN"]

    with pytest.raises(SemanticsStopped, match="PRODUCT_CONTROL_RESULT_INVALID") as caught:
        _validated_product_control_inventory(raw)

    assert "PRIVATE_DATE_FORMAT" not in json.dumps(_error_payload(caught.value))


def test_product_control_validator_rejects_unknown_categories_without_echoing_them() -> None:
    raw = product_control_payload()
    controls = cast(dict[str, dict[str, object]], raw["controls"])
    yesterday = controls["yesterday"]
    category_counts = cast(dict[str, int], yesterday["direct_interactive_category_counts"])
    category_counts["PRIVATE_SECRET_CATEGORY"] = 1

    with pytest.raises(SemanticsStopped, match="PRODUCT_CONTROL_RESULT_INVALID") as caught:
        _validated_product_control_inventory(raw)

    assert caught.value.safe_detail is None
    assert "PRIVATE_SECRET_CATEGORY" not in json.dumps(_error_payload(caught.value))


def test_cli_rejects_retired_tab_switch_before_config_or_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_if_loaded(*_args: object) -> None:
        raise AssertionError("config must not be loaded")

    monkeypatch.setattr(semantics_module, "_load_runtime", fail_if_loaded)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "read_only_product_business_semantics.py",
            "--config",
            "unused.toml",
            "--connection-id",
            "offline_test_connection",
            "--confirm-read-only",
            "--confirm-tab-switch",
        ],
    )

    assert main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "TAB_SWITCH_DISABLED_GLOBAL_ROUTE"


def test_tab_href_must_resolve_to_same_fixed_page() -> None:
    counts = SafetyCounts()
    safe_detail = asyncio.run(
        _tab_control_detail(cast(Any, FakeLocator(count=1, href="#transaction")), count=1)
    )
    asyncio.run(
        _require_interactive_tab(
            detail=safe_detail,
            missing_code="TRANSACTION_TAB_NOT_UNIQUE",
            counts=counts,
            all_details={"transaction": safe_detail},
        )
    )
    unsafe_detail = asyncio.run(
        _tab_control_detail(
            cast(Any, FakeLocator(count=1, href="https://evil.example/steal?secret=1#raw")),
            count=1,
        )
    )
    with pytest.raises(SemanticsStopped, match="TAB_CONTROL_HREF_UNSAFE") as caught:
        asyncio.run(
            _require_interactive_tab(
                detail=unsafe_detail,
                missing_code="TRANSACTION_TAB_NOT_UNIQUE",
                counts=counts,
                all_details={"transaction": unsafe_detail},
            )
        )
    serialized = json.dumps(caught.value.safe_detail, sort_keys=True)
    assert "evil.example" not in serialized
    assert "secret" not in serialized
    assert "#raw" not in serialized
    assert unsafe_detail["href_category"] == "EXTERNAL"


def test_same_origin_tab_inventory_outputs_only_sanitized_path() -> None:
    detail = asyncio.run(
        _tab_control_detail(
            cast(
                Any,
                FakeLocator(
                    count=1,
                    href="/sycm/123456789/detail?private_query=secret#private_fragment",
                ),
            ),
            count=1,
        )
    )

    assert detail["href_category"] == "SAME_ORIGIN_OTHER_PATH"
    assert detail["strict_pdd_origin_verified"] is True
    assert detail["sanitized_path"] == "/sycm/{id}/detail"
    serialized = json.dumps(detail, sort_keys=True)
    for forbidden in ("123456789", "private_query", "secret", "private_fragment"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("href", "category"),
    [
        (None, "MISSING"),
        ("", "EMPTY"),
        ("#transaction", "HASH_FRAGMENT"),
        ("javascript:void(0)", "JAVASCRIPT_VOID_0_EXACT"),
        (" javascript:void(0); ", "JAVASCRIPT_VOID_0_EXACT"),
        ("javascript:alert(1)", "JAVASCRIPT"),
        ("?tab=transaction", "SAME_ORIGIN_SAME_PATH"),
        ("/sycm/other", "SAME_ORIGIN_OTHER_PATH"),
        ("https://other.example/path", "EXTERNAL"),
        ("https://mms.pinduoduo.com:invalid/path", "INVALID"),
    ],
)
def test_tab_href_category_never_returns_href_text(href: str | None, category: str) -> None:
    assert _tab_href_category(href) == category


def test_programmatic_tab_switch_is_retired_before_any_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(
        TARGET_PAGE_URL,
        reload_responses=[list_response(query_type=0), ready_response()],
        transaction_responses=[list_response(query_type=1)],
        restore_responses=[list_response(query_type=0)],
    )
    identity = FakePage(IDENTITY_PAGE_URL, identity_text="店铺ID:123456789")
    manager = FakeManager(browser_with_required_pages(target, identity))
    monkeypatch.setattr(
        semantics_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )

    with pytest.raises(SemanticsStopped, match="TAB_SWITCH_DISABLED_GLOBAL_ROUTE") as caught:
        asyncio.run(
            semantics_inventory(
                runtime(),
                observe_seconds=0,
                catalog_product_ids=frozenset({"123456789"}),
                execute_tab_switch=True,
                min_action_interval_seconds=0,
            )
        )

    assert target.reload_calls == 1
    assert target.current_tab == "traffic"
    assert isinstance(caught.value.safe_detail, dict)
    safety = caught.value.counts
    assert safety.read_only_tab_clicks == 0
    assert safety.traffic_restore_clicks == 0
    assert safety.click_actions == 0
    assert safety.browser_close_actions == 0
    assert manager.stop_calls == 1


def test_catalog_match_metadata_and_counts_flow_through_baseline_without_new_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FakePage(
        TARGET_PAGE_URL,
        reload_responses=[list_response(), ready_response()],
    )
    identity = FakePage(IDENTITY_PAGE_URL, identity_text="店铺ID:123456789")
    manager = FakeManager(browser_with_required_pages(target, identity))
    monkeypatch.setattr(
        semantics_module,
        "async_playwright",
        lambda: FakePlaywrightStarter(manager),
    )
    metadata = {
        "requested": True,
        "manifest_record_count": 2,
        "platform_product_id_values_output": False,
    }

    result = asyncio.run(
        semantics_inventory(
            runtime(),
            observe_seconds=0,
            catalog_product_ids=frozenset({"123456789", "987654321"}),
            catalog_match_metadata=metadata,
        )
    )

    assert result["catalog_match"] == metadata
    listed = cast(dict[str, object], result["list"])
    products = cast(dict[str, object], listed["product_ids"])
    assert products["d2_match_evaluated"] is True
    assert products["d2_match_count"] == 1
    assert products["d2_unmatched_count"] == 0
    safety = cast(dict[str, int], result["safety_counts"])
    assert safety["reload_actions"] == 1
    assert safety["click_actions"] == 0
    assert safety["navigation_actions"] == 0
    assert safety["response_body_reads"] == 2
    assert target.reload_calls == 1
    assert manager.stop_calls == 1


def test_cli_catalog_flag_loads_internal_binding_and_passes_only_ids_to_baseline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    metadata = {
        "requested": True,
        "manifest_record_count": 2,
        "platform_product_id_values_output": False,
    }

    async def fake_semantics(received_runtime: object, **kwargs: object) -> dict[str, object]:
        captured["runtime"] = received_runtime
        captured.update(kwargs)
        return {"status": "PASS", "catalog_match": kwargs["catalog_match_metadata"]}

    monkeypatch.setattr(semantics_module, "_load_runtime", lambda *_args: runtime())
    monkeypatch.setattr(
        semantics_module,
        "_load_latest_product_catalog",
        lambda _runtime: (frozenset({"123456789", "987654321"}), metadata),
    )
    monkeypatch.setattr(semantics_module, "semantics_inventory", fake_semantics)
    monkeypatch.setattr(
        semantics_module,
        "async_playwright",
        lambda: (_ for _ in ()).throw(AssertionError("browser must not start in fake baseline")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "read_only_product_business_semantics.py",
            "--config",
            "trusted.toml",
            "--connection-id",
            "conn_current",
            "--confirm-read-only",
            "--match-latest-product-catalog",
        ],
    )

    assert main() == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["catalog_match"] == metadata
    assert captured["catalog_product_ids"] == frozenset({"123456789", "987654321"})
    assert captured["catalog_match_metadata"] == metadata
    assert "CATALOG_LOAD_BEGIN" in output.err
    assert "CATALOG_LOAD_DONE" in output.err
    assert "123456789" not in output.out
    assert "987654321" not in output.out


def test_catalog_match_flag_rejects_inventory_modes_before_catalog_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        semantics_module,
        "_load_latest_product_catalog",
        lambda _runtime: (_ for _ in ()).throw(AssertionError("catalog must not be read")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "read_only_product_business_semantics.py",
            "--config",
            "unused.toml",
            "--connection-id",
            "conn_current",
            "--confirm-read-only",
            "--inventory-product-controls-only",
            "--match-latest-product-catalog",
        ],
    )

    assert main() == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "INCOMPATIBLE_MODE_FLAGS"


def test_main_without_confirmation_stops_before_config_or_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_load_config(path: object) -> None:
        del path
        raise AssertionError("must stop before config and browser")

    monkeypatch.setattr(semantics_module, "load_config", forbidden_load_config)
    monkeypatch.setattr(
        "sys.argv",
        [
            "read_only_product_business_semantics.py",
            "--config",
            "unused.toml",
            "--connection-id",
            "conn_current",
        ],
    )
    assert semantics_module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "READ_ONLY_CONFIRMATION_REQUIRED"
    assert payload["safety_counts"]["response_body_reads"] == 0
