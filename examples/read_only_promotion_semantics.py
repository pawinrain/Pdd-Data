from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page, Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url

CDP_ENDPOINT = "http://127.0.0.1:9222"
PROMOTION_URL = "https://yingxiao.pinduoduo.com/goods/promotion/list"
PRODUCT_URL = "https://mms.pinduoduo.com/goods/goods_list"
PROMOTION_LIST_PATH = "/mms-gateway/venus/api/goods/promotion/v3/list"
PROMOTION_IDENTITY_PATH = "/mms-gateway/venus/api/user/userInfo"
PRODUCT_LIST_PATH = "/vodka/v2/mms/query/display/mall/goodsList"
MAX_RESPONSE_BYTES = 1_048_576

SELECTED_METRICS = (
    "spend",
    "orderSpend",
    "orderSpendRoiUnified",
    "orderSpendNetRoi",
    "netOrderNum",
    "orderNum",
    "gmv",
    "netGmv",
    "impression",
    "click",
    "settlementRoi",
    "settlementOrder",
)
CONFIGURATION_FIELDS = ("maxCost", "targetRoi", "agentBid", "adStatus")
KNOWN_STATUS_LABELS = ("推广中", "已暂停", "已结束", "审核中", "审核驳回", "已删除")


def _matches(host: str, path: str) -> Callable[[Response], bool]:
    def matches(response: Response) -> bool:
        parsed = urlsplit(response.url)
        return (
            parsed.hostname == host
            and parsed.path == path
            and response.request.method == "POST"
            and response.status == 200
            and response.headers.get("content-type", "").partition(";")[0].casefold()
            == "application/json"
        )

    return matches


async def _body(response: Response) -> dict[str, Any]:
    raw = await response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("verified response exceeds one MiB")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("verified response is not an object")
    return value


def _fingerprint(value: object) -> str | None:
    if not isinstance(value, str | int):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _id_summary(rows: list[dict[str, Any]], key: str) -> dict[str, object]:
    values = [row.get(key) for row in rows]
    normalized = [str(value) for value in values if isinstance(value, str | int)]
    return {
        "present_on_all_rows": len(normalized) == len(rows),
        "unique_count": len(set(normalized)),
        "types": sorted({type(value).__name__ for value in values}),
    }


def _unit_summary(rows: list[dict[str, Any]], container: str, key: str) -> dict[str, object]:
    units: set[str] = set()
    unit_codes: set[int] = set()
    value_types: set[str] = set()
    present = 0
    missing = 0
    for row in rows:
        parent = row.get(container) if container else row
        value = parent.get(key) if isinstance(parent, dict) else None
        if value is None:
            missing += 1
            continue
        present += 1
        value_types.add(type(value).__name__)
        if isinstance(value, dict):
            if isinstance(value.get("unit"), str):
                units.add(value["unit"])
            if isinstance(value.get("unitCode"), int):
                unit_codes.add(value["unitCode"])
    return {
        "present_count": present,
        "missing_count": missing,
        "units": sorted(units),
        "unit_codes": sorted(unit_codes),
        "value_types": sorted(value_types),
    }


def _timestamp_summary(value: object) -> dict[str, object]:
    if isinstance(value, bool) or not isinstance(value, int):
        return {"type": type(value).__name__, "epoch_unit": None, "plausible": False}
    digits = len(str(abs(value)))
    divisor = 1_000 if digits == 13 else 1 if digits == 10 else None
    if divisor is None:
        return {"type": "int", "epoch_unit": None, "plausible": False}
    try:
        parsed = datetime.fromtimestamp(value / divisor, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return {"type": "int", "epoch_unit": None, "plausible": False}
    return {
        "type": "int",
        "epoch_unit": "MILLISECONDS" if divisor == 1_000 else "SECONDS",
        "plausible": 2020 <= parsed.year <= 2100,
    }


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def _pages(browser: Any) -> tuple[Page, Page]:
    pages = [page for context in browser.contexts for page in context.pages]
    promotion = next((page for page in pages if same_page_url(page.url, PROMOTION_URL)), None)
    product = next((page for page in pages if same_page_url(page.url, PRODUCT_URL)), None)
    if promotion is None or product is None:
        raise RuntimeError("required promotion and product pages must both be open")
    return promotion, product


async def verify(expected_sha256: str) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(CDP_ENDPOINT)
    try:
        promotion_page, product_page = await _pages(browser)
        async with (
            promotion_page.expect_response(
                _matches("yingxiao.pinduoduo.com", PROMOTION_LIST_PATH), timeout=20_000
            ) as promotion_info,
            promotion_page.expect_response(
                _matches("yingxiao.pinduoduo.com", PROMOTION_IDENTITY_PATH), timeout=20_000
            ) as identity_info,
        ):
            await promotion_page.reload(wait_until="domcontentloaded", timeout=20_000)
        promotion_response = await promotion_info.value
        identity_response = await identity_info.value
        promotion_payload = await _body(promotion_response)
        identity_payload = await _body(identity_response)

        async with product_page.expect_response(
            _matches("mms.pinduoduo.com", PRODUCT_LIST_PATH), timeout=20_000
        ) as product_info:
            await product_page.reload(wait_until="domcontentloaded", timeout=20_000)
        product_payload = await _body(await product_info.value)

        promotion_result = promotion_payload.get("result")
        identity_result = identity_payload.get("result")
        product_result = product_payload.get("result")
        rows_value = promotion_result.get("adInfos") if isinstance(promotion_result, dict) else None
        product_rows_value = (
            product_result.get("goods_list") if isinstance(product_result, dict) else None
        )
        if (
            promotion_payload.get("success") is not True
            or identity_payload.get("success") is not True
            or product_payload.get("success") is not True
            or not isinstance(rows_value, list)
            or not isinstance(product_rows_value, list)
        ):
            raise RuntimeError("one or more verified responses failed their business gate")
        if not all(isinstance(row, dict) for row in rows_value):
            raise RuntimeError("promotion list contains a non-object row")
        if not all(isinstance(row, dict) for row in product_rows_value):
            raise RuntimeError("product list contains a non-object row")
        rows: list[dict[str, Any]] = rows_value
        product_rows: list[dict[str, Any]] = product_rows_value

        mall = identity_result.get("mall") if isinstance(identity_result, dict) else None
        identity_fingerprint = _fingerprint(mall.get("mallId") if isinstance(mall, dict) else None)
        row_fingerprints = {_fingerprint(row.get("mallId")) for row in rows}
        row_fingerprints.discard(None)

        catalog_ids = {
            str(row["id"]) for row in product_rows if isinstance(row.get("id"), str | int)
        }
        promoted_goods_ids = {
            str(row["goodsId"]) for row in rows if isinstance(row.get("goodsId"), str | int)
        }

        request = promotion_response.request.post_data_json
        if not isinstance(request, dict):
            raise RuntimeError("promotion request body is not an object")
        begin_date = _parse_iso_date(request.get("beginDate"))
        end_date = _parse_iso_date(request.get("endDate"))
        request_contract_fields = (
            "blockType",
            "clientType",
            "orderBy",
            "scenesMode",
            "showGoodsPromotionHistoryReport",
            "sortBy",
            "withTagsInfo",
        )

        dom_rows = await promotion_page.locator("tbody tr").all_text_contents()
        normalized_dom_rows = ["".join(text.split()) for text in dom_rows]
        names = []
        for row in rows:
            goods = row.get("goodsInfo")
            name = goods.get("goodsName") if isinstance(goods, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
        unique_dom_name_matches = sum(
            sum(name in dom_row for dom_row in normalized_dom_rows) == 1 for name in names
        )

        status_dom_labeled = 0
        for row in rows:
            goods = row.get("goodsInfo")
            name = goods.get("goodsName") if isinstance(goods, dict) else None
            matched_dom = next(
                (
                    dom_row
                    for dom_row in normalized_dom_rows
                    if isinstance(name, str) and name in dom_row
                ),
                "",
            )
            if any(label in matched_dom for label in KNOWN_STATUS_LABELS):
                status_dom_labeled += 1

        sum_report = (
            promotion_result.get("sumReportInfo") if isinstance(promotion_result, dict) else None
        )
        sum_rows = [sum_report] if isinstance(sum_report, dict) else []
        report_last_update = (
            promotion_result.get("reportLastUpdateTime")
            if isinstance(promotion_result, dict)
            else None
        )

        result = {
            "status": "PASS",
            "identity": {
                "promotion_identity_matches_expected": identity_fingerprint == expected_sha256,
                "all_rows_have_one_expected_store": row_fingerprints == {expected_sha256},
                "identity_value_printed": False,
            },
            "request_window": {
                "begin_date_present": begin_date is not None,
                "end_date_present": end_date is not None,
                "inclusive_days": (
                    (end_date - begin_date).days + 1
                    if begin_date is not None and end_date is not None and end_date >= begin_date
                    else None
                ),
                "page_number": request.get("pageNumber")
                if isinstance(request.get("pageNumber"), int)
                else None,
                "page_size": request.get("pageSize")
                if isinstance(request.get("pageSize"), int)
                else None,
            },
            "request_contract": {
                "keys": sorted(request),
                "filter_is_empty_object": request.get("filter") == {},
                "crawler_info": {
                    "type": type(request.get("crawlerInfo")).__name__,
                    "present": bool(request.get("crawlerInfo")),
                    "value_printed": False,
                },
                "safe_constants": {
                    field: request.get(field)
                    for field in request_contract_fields
                    if isinstance(request.get(field), bool | int)
                },
                "page_query_present": bool(urlsplit(promotion_page.url).query),
                "search_or_filter_values_printed": False,
            },
            "entities": {
                "record_count": len(rows),
                "ad_id": _id_summary(rows, "adId"),
                "plan_id": _id_summary(rows, "planId"),
                "goods_id": _id_summary(rows, "goodsId"),
                "promoted_goods_are_catalog_subset": promoted_goods_ids <= catalog_ids,
                "catalog_product_count": len(catalog_ids),
                "promoted_product_count": len(promoted_goods_ids),
                "dom_table_row_count": len(dom_rows),
                "unique_dom_product_name_matches": unique_dom_name_matches,
                "product_names_printed": False,
            },
            "metrics": {key: _unit_summary(rows, "reportInfo", key) for key in SELECTED_METRICS},
            "sum_metrics": {
                key: _unit_summary(sum_rows, "", key)
                if sum_rows
                else {"present_count": 0, "missing_count": 1, "units": [], "unit_codes": []}
                for key in SELECTED_METRICS
            },
            "current_configuration": {
                "max_cost": _unit_summary(rows, "", "maxCost"),
                "target_roi": _unit_summary(rows, "", "targetRoi"),
                "agent_bid": _unit_summary(rows, "", "agentBid"),
                "ad_status": _id_summary(rows, "adStatus"),
                "dom_status_label_count": status_dom_labeled,
                "configuration_values_printed": False,
            },
            "source_update": {
                "present": report_last_update is not None,
                "classification": _timestamp_summary(report_last_update),
            },
            "raw_bodies_persisted": False,
            "business_values_printed": False,
        }
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    finally:
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only promotion semantics gate.")
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256):
        parser.error("--expected-sha256 must be a lowercase SHA-256 digest")
    asyncio.run(verify(args.expected_sha256))


if __name__ == "__main__":
    main()
