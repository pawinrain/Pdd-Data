from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pdd_data_mcp.browser.aftersale_parsing import parse_aftersale_list_response
from pdd_data_mcp.config import AftersaleAdapterSettings
from pdd_data_mcp.contracts.models import DatasetType, WindowKind
from pdd_data_mcp.errors import CollectionRejected


def _adapter() -> AftersaleAdapterSettings:
    return AftersaleAdapterSettings(
        verified=False,
        data_source="NETWORK_RESPONSE",
        target_page_url="https://mms.pinduoduo.com/aftersales/aftersale_list?searchType=7",
        response_host="mms.pinduoduo.com",
        response_path="/mercury/mms/afterSales/queryList",
        response_method="POST",
        business_success_path="success",
        business_success_value=True,
        list_path="result.list",
        total_path="result.total",
        aftersale_id_path="id",
        order_sn_path="orderSn",
        aftersale_type_path="afterSalesType",
        aftersale_type_name_path="afterSalesTypeName",
        aftersale_status_path="afterSalesStatus",
        aftersale_title_path="afterSalesTitle",
        refund_amount_path="refundAmount",
        order_amount_path="orderAmount",
        goods_name_path="goodsName",
        goods_spec_path="goodsSpec",
        goods_number_path="goodsNumber",
        reason_desc_path="afterSalesReasonDesc",
        expire_remain_path="expireRemainTime",
        created_at_path="createdAt",
        supported_windows=["POINT_IN_TIME"],
        parser_version="pdd-aftersale-orders/0.1.0",
    )


def test_parse_aftersale_list_row_from_evidence() -> None:
    payload = {
        "success": True,
        "errorCode": 1000000,
        "result": {
            "total": 1,
            "list": [
                {
                    "id": 22750431628747,
                    "afterSalesType": 2,
                    "afterSalesTypeName": "退货退款",
                    "afterSalesStatus": 11,
                    "createdAt": 1789175147,
                    "refundAmount": 1296,
                    "orderAmount": 1296,
                    "orderSn": "260909-672850297221479",
                    "goodsName": "大容量双肩包",
                    "goodsNumber": 1,
                    "goodsSpec": "活力橙",
                    "afterSalesTitle": "退货退款，待商家确认收货",
                    "afterSalesReasonDesc": "不想要了",
                    "expireRemainTime": 599959,
                }
            ],
        },
    }
    records, total = parse_aftersale_list_response(
        payload, adapter=_adapter(), observed_at=datetime(2026, 9, 12, 2, 0, tzinfo=UTC)
    )
    assert total == 1
    assert len(records) == 1
    row = records[0]
    assert row.aftersale_id == "22750431628747"
    assert row.order_sn == "260909-672850297221479"
    assert row.refund_amount_cents == 1296
    assert row.aftersale_type_name == "退货退款"
    assert row.missing_reasons == {}


def test_dataset_type_exists() -> None:
    assert DatasetType.AFTERSALE_ORDERS.value == "aftersale_orders"
    assert WindowKind.POINT_IN_TIME.value == "POINT_IN_TIME"
