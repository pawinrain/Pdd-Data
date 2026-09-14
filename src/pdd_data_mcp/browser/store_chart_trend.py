"""Parse MMS home realtime chart payload into StoreChartTrend.

Source: POST /sydney/api/mallCoreData/homePageOverView
"""

from __future__ import annotations

import json
from typing import Any

from pdd_data_mcp.contracts.models import StoreChartTrend, StoreTrendPoint


HOME_PAGE_OVERVIEW_PATH = "/sydney/api/mallCoreData/homePageOverView"


def _as_nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value != value or value < 0:  # noqa: PLR0124 — NaN check
            return None
        return int(round(value))
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if num != num or num < 0:
        return None
    return int(round(num))


def _money_cents(value: Any) -> int | None:
    # homePageOverView amounts are already in 分 (e.g. 16720 == ¥167.20)
    return _as_nonneg_int(value)


def _diff_series(values: list[int | None]) -> list[int | None]:
    """Convert cumulative series to per-bucket increments."""
    out: list[int | None] = []
    prev = 0
    for value in values:
        if value is None:
            out.append(None)
            continue
        out.append(max(0, value - prev))
        prev = value
    return out


def _hourly_from_lists(
    *,
    per_hour: list[dict[str, Any]],
    cumulative: list[dict[str, Any]],
) -> list[StoreTrendPoint]:
    by_hour_amt: dict[str, dict[str, Any]] = {}
    for row in per_hour:
        if not isinstance(row, dict):
            continue
        hr = str(row.get("hr") or "").zfill(2)
        if not hr.isdigit():
            continue
        by_hour_amt[hr] = row

    cum_by_hour: dict[str, dict[str, Any]] = {}
    for row in cumulative:
        if not isinstance(row, dict):
            continue
        hr = str(row.get("hr") or "").zfill(2)
        if not hr.isdigit():
            continue
        cum_by_hour[hr] = row

    hours = sorted(set(by_hour_amt) | set(cum_by_hour), key=lambda h: int(h))
    visitor_cum = [_as_nonneg_int((cum_by_hour.get(h) or {}).get("guvOned")) for h in hours]
    page_cum = [_as_nonneg_int((cum_by_hour.get(h) or {}).get("gpvOned")) for h in hours]
    spend_cum = [_money_cents((cum_by_hour.get(h) or {}).get("promotionSpend")) for h in hours]
    visitor_inc = _diff_series(visitor_cum)
    page_inc = _diff_series(page_cum)
    spend_inc = _diff_series(spend_cum)

    points: list[StoreTrendPoint] = []
    for index, hour in enumerate(hours):
        row = by_hour_amt.get(hour) or {}
        points.append(
            StoreTrendPoint(
                key=hour,
                gmv_cents=_money_cents(row.get("curPayOrdrAmt")),
                order_count=_as_nonneg_int(row.get("curPayOrdrCnt")),
                visitor_count=visitor_inc[index] if index < len(visitor_inc) else None,
                page_view_count=page_inc[index] if index < len(page_inc) else None,
                review_count=None,
                ad_spend_cents=spend_inc[index] if index < len(spend_inc) else None,
            )
        )
    return points


def _day_value(row: dict[str, Any], primary: str, fallback: str) -> int | None:
    value = _as_nonneg_int(row.get(primary))
    if value is not None:
        return value
    return _as_nonneg_int(row.get(fallback))


def _day_money(row: dict[str, Any], primary: str, fallback: str) -> int | None:
    value = _money_cents(row.get(primary))
    if value is not None:
        return value
    return _money_cents(row.get(fallback))


def _daily_from_month(
    month_trend: list[dict[str, Any]],
    review_by_date: dict[str, int | None],
    *,
    last_n: int | None = None,
) -> list[StoreTrendPoint]:
    rows = [row for row in month_trend if isinstance(row, dict) and row.get("statDate")]
    if last_n is not None:
        rows = rows[-last_n:]
    points: list[StoreTrendPoint] = []
    for row in rows:
        date_key = str(row.get("statDate"))
        points.append(
            StoreTrendPoint(
                key=date_key,
                gmv_cents=_day_money(row, "curPayOrdrAmt", "payOrdrAmtOst"),
                order_count=_day_value(row, "curPayOrdrCnt", "payOrdrCntOst"),
                visitor_count=_day_value(row, "guvOned", "guvOst"),
                page_view_count=_day_value(row, "gpvOned", "gpvOst"),
                review_count=review_by_date.get(date_key),
                ad_spend_cents=_money_cents(row.get("promotionSpend")),
            )
        )
    return points


def _append_open_hour(
    today: list[StoreTrendPoint],
    *,
    overview: dict[str, Any] | None,
    rt_trend: list[dict[str, Any]],
) -> list[StoreTrendPoint]:
    """官方分时列表常滞后 1 个未闭合小时；用汇总 − 累计末点补当前小时。"""
    if not isinstance(overview, dict):
        return today
    last_rt = rt_trend[-1] if rt_trend else None
    if not isinstance(last_rt, dict):
        return today
    try:
        last_hr = int(str(last_rt.get("hr")))
    except (TypeError, ValueError):
        return today
    open_hr = last_hr + 1
    if open_hr > 23:
        return today
    if today and any(p.key == f"{open_hr:02d}" for p in today):
        return today

    def delta(overview_key: str, rt_key: str, money: bool = False) -> int | None:
        left = _money_cents(overview.get(overview_key)) if money else _as_nonneg_int(overview.get(overview_key))
        right = _money_cents(last_rt.get(rt_key)) if money else _as_nonneg_int(last_rt.get(rt_key))
        if left is None or right is None:
            return None
        return max(0, left - right)

    gmv = delta("curPayOrdrAmt", "curPayOrdrAmt", money=True)
    orders = delta("curPayOrdrCnt", "curPayOrdrCnt")
    visitors = delta("guvOned", "guvOned")
    views = delta("gpvOned", "gpvOned")
    spend = delta("promotionSpend", "promotionSpend", money=True)
    if all(v in (None, 0) for v in (gmv, orders, visitors, views, spend)):
        return today

    return [
        *today,
        StoreTrendPoint(
            key=f"{open_hr:02d}",
            gmv_cents=gmv,
            order_count=orders,
            visitor_count=visitors,
            page_view_count=views,
            review_count=None,
            ad_spend_cents=spend,
        ),
    ]


def parse_home_page_overview(raw: bytes | str | dict[str, Any]) -> StoreChartTrend | None:
    if isinstance(raw, bytes | str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        payload = raw
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    today_list = result.get("todayPerHourRtList") or []
    yesterday_list = result.get("yesterdayPerHourRtList") or []
    rt_trend = result.get("rtTrend") or []
    yesterday_trend = result.get("yesterdayTrend") or []
    month_trend = result.get("monthTrend") or []
    week_reviews = result.get("weekGoodsReviewCntTrend") or []

    if not isinstance(today_list, list) or not isinstance(month_trend, list):
        return None

    review_by_date: dict[str, int | None] = {}
    if isinstance(week_reviews, list):
        for row in week_reviews:
            if isinstance(row, dict) and row.get("statDate"):
                review_by_date[str(row["statDate"])] = _as_nonneg_int(row.get("goodsReviewCnt"))

    rt_rows = [row for row in rt_trend if isinstance(row, dict)]
    today = _hourly_from_lists(
        per_hour=[row for row in today_list if isinstance(row, dict)],
        cumulative=rt_rows,
    )
    overview = result.get("rtDataOverView")
    today = _append_open_hour(
        today,
        overview=overview if isinstance(overview, dict) else None,
        rt_trend=rt_rows,
    )
    yesterday = _hourly_from_lists(
        per_hour=[row for row in yesterday_list if isinstance(row, dict)],
        cumulative=[row for row in yesterday_trend if isinstance(row, dict)],
    )
    month_rows = [row for row in month_trend if isinstance(row, dict)]
    week7 = _daily_from_month(month_rows, review_by_date, last_n=7)
    month30 = _daily_from_month(month_rows, review_by_date, last_n=None)

    if not today and not yesterday and not week7 and not month30:
        return None

    return StoreChartTrend(
        source_path=HOME_PAGE_OVERVIEW_PATH,
        today=today,
        yesterday=yesterday,
        week7=week7,
        month30=month30,
    )
