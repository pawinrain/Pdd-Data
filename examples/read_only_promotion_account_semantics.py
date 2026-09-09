from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Response, async_playwright

from pdd_data_mcp.browser.cdp import same_page_url
from pdd_data_mcp.browser.promotion import sanitized_json_shape

CDP_ENDPOINT = "http://127.0.0.1:9222"
TARGET_URL = "https://yingxiao.pinduoduo.com/mains/promotionOverview"
HOST = "yingxiao.pinduoduo.com"
REPORT_PATH = "/mms-gateway/poseidon/api/report/queryHourlyRangeReport"
IDENTITY_PATH = "/mms-gateway/venus/api/user/info"
MAX_RESPONSE_BYTES = 1_048_576
BUSINESS_TIMEZONE = "Asia/Shanghai"
ZONE = ZoneInfo(BUSINESS_TIMEZONE)
PLATFORM_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SEMANTICS = "SAME_PERIOD_COMPARISON"
DATE_EXPLANATION_SELECTOR = "div[class*='ReportDateExplain_content__']"
DATE_EXPLANATION_PATTERN = re.compile(
    r"\*今日截至(?P<today_hour>[01]\d|2[0-3]):(?P<today_minute>[0-5]\d)的数据\uff1b"
    r"昨日截至(?P<yesterday_hour>[01]\d|2[0-3]):(?P<yesterday_minute>[0-5]\d)的数据"
)
TODAY_REQUEST_KEYS = frozenset(
    {
        "blockTypes",
        "clientType",
        "crawlerInfo",
        "endDate",
        "endDayHour",
        "entityId",
        "queryDimensionType",
        "reportPromotionType",
        "returnLastUpdateTime",
        "startDate",
    }
)
YESTERDAY_REQUEST_KEYS = TODAY_REQUEST_KEYS - {"returnLastUpdateTime"}
MISSING = object()
SAFE_HOURLY_TIME_KEYS = frozenset(
    {"date", "hour", "time", "hourBegin", "hourEnd", "startTime", "endTime"}
)
SAFE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_MIDNIGHT_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} 00:00:00$")
SAFE_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")


def _matches(response: Response, path: str) -> bool:
    parsed = urlsplit(response.url)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == HOST.casefold()
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.path == path
        and not parsed.query
        and not parsed.fragment
        and response.request.method == "POST"
        and response.status == 200
        and response.headers.get("content-type", "").partition(";")[0].casefold()
        == "application/json"
    )


async def _body(response: Response) -> dict[str, Any]:
    raw = await response.body()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("verified response exceeds one MiB")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("verified response is not an object")
    return value


def _fingerprint(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _platform_midnight_date(value: object, field: str) -> date:
    if not isinstance(value, str) or len(value) != 19:
        raise RuntimeError(f"{field} is not a canonical platform date")
    try:
        parsed = datetime.strptime(value, PLATFORM_DATE_FORMAT)
    except ValueError as exc:
        raise RuntimeError(f"{field} is not a canonical platform date") from exc
    if parsed.strftime(PLATFORM_DATE_FORMAT) != value or any(
        (parsed.hour, parsed.minute, parsed.second, parsed.microsecond)
    ):
        raise RuntimeError(f"{field} is not canonical midnight")
    return parsed.date()


def _parse_page_date_explanation(value: object) -> dict[str, str]:
    if not isinstance(value, str):
        raise RuntimeError("account page date explanation is unavailable")
    match = DATE_EXPLANATION_PATTERN.fullmatch("".join(value.split()))
    if match is None:
        raise RuntimeError("account page date explanation is not canonical")
    return {
        "today_cutoff_hhmm": f"{match.group('today_hour')}:{match.group('today_minute')}",
        "yesterday_cutoff_hhmm": (
            f"{match.group('yesterday_hour')}:{match.group('yesterday_minute')}"
        ),
        "page_semantics": PAGE_SEMANTICS,
        "business_timezone": BUSINESS_TIMEZONE,
    }


def _unix_milliseconds(value: object, field: str) -> datetime:
    if type(value) is not int or len(str(value)) != 13:
        raise RuntimeError(f"{field} is not Unix milliseconds")
    seconds, milliseconds = divmod(value, 1000)
    try:
        parsed = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=milliseconds * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError(f"{field} is outside the supported timestamp range") from exc
    if not 2020 <= parsed.year <= 2100:
        raise RuntimeError(f"{field} is outside the supported timestamp range")
    return parsed


def _request_time_summary(request: object, *, current_date: date) -> dict[str, object]:
    if not isinstance(request, dict):
        raise RuntimeError("account report request is not an object")
    keys = frozenset(request)
    if keys == TODAY_REQUEST_KEYS:
        window_kind = "TODAY"
        expected_date = current_date
    elif keys == YESTERDAY_REQUEST_KEYS:
        window_kind = "YESTERDAY"
        expected_date = current_date - timedelta(days=1)
    else:
        raise RuntimeError("account report request keys do not match the reviewed contract")
    start_date = _platform_midnight_date(request.get("startDate"), "request startDate")
    end_date = _platform_midnight_date(request.get("endDate"), "request endDate")
    if start_date != expected_date or end_date != expected_date:
        raise RuntimeError("account report request business date mismatch")
    block_types = request.get("blockTypes")
    if (
        type(block_types) is not list
        or len(block_types) != 1
        or type(block_types[0]) is not int
        or block_types[0] != 1
    ):
        raise RuntimeError("account report request blockTypes mismatch")
    for field, expected in (
        ("clientType", 1),
        ("queryDimensionType", 0),
        ("reportPromotionType", 9),
    ):
        actual = request.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise RuntimeError(f"account report request {field} mismatch")
    if window_kind == "TODAY" and request.get("returnLastUpdateTime") is not True:
        raise RuntimeError("account TODAY request update-time flag mismatch")
    crawler_info = request.get("crawlerInfo")
    if not isinstance(crawler_info, str) or not crawler_info.strip() or len(crawler_info) > 4096:
        raise RuntimeError("account report crawlerInfo shape mismatch")
    end_day_hour = request.get("endDayHour")
    if type(end_day_hour) is not int or not 0 <= end_day_hour <= 23:
        raise RuntimeError("account report endDayHour is invalid")
    return {
        "window_kind": window_kind,
        "business_date": expected_date.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "end_day_hour": end_day_hour,
    }


def _response_time_summary(
    payload: object, *, window_kind: str, expected_business_date: date
) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError("account report response gate failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("account report result is not an object")
    daily = result.get("dailyReportList")
    if not isinstance(daily, list) or len(daily) != 1 or not isinstance(daily[0], dict):
        raise RuntimeError("account report must contain exactly one daily row")
    daily_business_date = _platform_midnight_date(daily[0].get("date"), "response daily date")
    if daily_business_date != expected_business_date:
        raise RuntimeError("account response daily business date mismatch")
    hourly = result.get("hourlyReportList")
    if not isinstance(hourly, list):
        raise RuntimeError("account response hourlyReportList is not an array")
    primary = result.get("reportLastUpdateTime", MISSING)
    secondary = result.get("lastUpdateTime", MISSING)
    if primary is MISSING or secondary is MISSING:
        raise RuntimeError("account response source update field is missing")
    if window_kind == "YESTERDAY":
        if primary is not None or secondary is not None:
            raise RuntimeError("account YESTERDAY source updates are not both null")
        source_update_timestamp = None
        source_update_missing_status = "SOURCE_VALUE_NULL"
    elif window_kind == "TODAY":
        parsed_primary = _unix_milliseconds(primary, "reportLastUpdateTime")
        parsed_secondary = _unix_milliseconds(secondary, "lastUpdateTime")
        if parsed_primary != parsed_secondary:
            raise RuntimeError("account response source update timestamps disagree")
        source_update_timestamp = parsed_primary.astimezone(ZONE).isoformat()
        source_update_missing_status = None
    else:
        raise RuntimeError("account response window kind is unsupported")
    return {
        "daily_business_date": daily_business_date.isoformat(),
        "daily_row_count": 1,
        "hourly_row_count": len(hourly),
        "hourly_row_shape": (
            sanitized_json_shape(hourly[0], max_depth=6, max_entries=200) if hourly else []
        ),
        "hourly_temporal_evidence": _hourly_temporal_evidence(hourly),
        "source_update_timestamp": source_update_timestamp,
        "source_update_missing_status": source_update_missing_status,
    }


def _safe_hourly_time_value(value: object) -> int | str | None:
    if type(value) is int:
        return value if 0 <= value <= 23 else None
    if not isinstance(value, str):
        return None
    if SAFE_DATE_PATTERN.fullmatch(value):
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        return parsed_date.isoformat() if parsed_date.isoformat() == value else None
    if SAFE_MIDNIGHT_DATE_PATTERN.fullmatch(value):
        try:
            return _platform_midnight_date(value, "hourly date").isoformat()
        except RuntimeError:
            return None
    return value if SAFE_TIME_PATTERN.fullmatch(value) else None


def _hourly_temporal_evidence(hourly: list[object]) -> dict[str, object]:
    if not hourly or not isinstance(hourly[0], dict) or not isinstance(hourly[-1], dict):
        return {"found": False}
    first = hourly[0]
    last = hourly[-1]
    first_keys = set(first).intersection(SAFE_HOURLY_TIME_KEYS)
    last_keys = set(last).intersection(SAFE_HOURLY_TIME_KEYS)
    if not first_keys or first_keys != last_keys:
        return {"found": False}
    first_safe: dict[str, int | str] = {}
    last_safe: dict[str, int | str] = {}
    for key in sorted(first_keys):
        first_value = _safe_hourly_time_value(first[key])
        last_value = _safe_hourly_time_value(last[key])
        if first_value is None or last_value is None:
            return {"found": False}
        first_safe[key] = first_value
        last_safe[key] = last_value
    return {"found": True, "first": first_safe, "last": last_safe}


def _yuan_cents(value: object) -> int:
    if (
        not isinstance(value, dict)
        or value.get("unit") != "YUAN"
        or type(value.get("unitCode")) is not int
        or value.get("unitCode") != 1
        or not isinstance(value.get("value"), str)
    ):
        raise RuntimeError("account spend unit is not verified")
    try:
        cents = Decimal(value["value"]) * 100
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError("account spend value is invalid") from exc
    if not cents.is_finite() or cents < 0 or cents != cents.to_integral_value():
        raise RuntimeError("account spend is not exact nonnegative cents")
    return int(cents)


async def inspect(expected_sha256: str, *, restore_error_tab: bool) -> None:
    manager = await async_playwright().start()
    browser = await manager.chromium.connect_over_cdp(CDP_ENDPOINT)
    try:
        pages = [page for context in browser.contexts for page in context.pages]
        matching = [page for page in pages if same_page_url(page.url, TARGET_URL)]
        error_tab_restored = False
        if not matching and restore_error_tab:
            error_pages = [page for page in pages if urlsplit(page.url).scheme == "chrome-error"]
            if len(error_pages) != 1:
                raise RuntimeError("exactly one failed Chrome tab is required for safe restoration")
            page = error_pages[0]
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20_000)
            if not same_page_url(page.url, TARGET_URL):
                raise RuntimeError("promotion overview tab restoration failed")
            matching = [page]
            error_tab_restored = True
        if len(matching) != 1:
            raise RuntimeError("exactly one promotion overview page must be open")
        page = matching[0]
        reports: list[Response] = []
        identities: list[Response] = []

        def observe(response: Response) -> None:
            if _matches(response, REPORT_PATH):
                reports.append(response)
            elif _matches(response, IDENTITY_PATH):
                identities.append(response)

        page.on("response", observe)
        capture_before = datetime.now(ZONE)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(5_000)
        finally:
            page.remove_listener("response", observe)
        capture_after = datetime.now(ZONE)
        if capture_before.date() != capture_after.date():
            raise RuntimeError("account semantics probe crossed the business-date boundary")
        if len(reports) != 2 or len(identities) != 1:
            raise RuntimeError("exactly two account reports and one identity response are required")
        identity_payload = await _body(identities[0])
        identity_result = identity_payload.get("result")
        identity_id = identity_result.get("mallId") if isinstance(identity_result, dict) else None
        if (
            identity_payload.get("success") is not True
            or _fingerprint(identity_id) != expected_sha256
        ):
            raise RuntimeError("identity mismatch")

        explanation = page.locator(DATE_EXPLANATION_SELECTOR)
        explanation_count = await explanation.count()
        if explanation_count != 1:
            raise RuntimeError("account page date explanation is not unique")
        page_time_scope = _parse_page_date_explanation(await explanation.first.text_content())

        summaries: list[dict[str, object]] = []
        spend_by_date: dict[date, int] = {}
        observed_windows: set[str] = set()
        for response in reports:
            request = response.request.post_data_json
            payload = await _body(response)
            request_summary = _request_time_summary(
                request,
                current_date=capture_before.date(),
            )
            window_kind = str(request_summary["window_kind"])
            if window_kind in observed_windows:
                raise RuntimeError("duplicate account report window")
            observed_windows.add(window_kind)
            if (
                not isinstance(request, dict)
                or _fingerprint(request.get("entityId")) != expected_sha256
            ):
                raise RuntimeError("account report request identity mismatch")
            request_business_date = date.fromisoformat(str(request_summary["business_date"]))
            response_summary = _response_time_summary(
                payload,
                window_kind=window_kind,
                expected_business_date=request_business_date,
            )
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("account report result missing")
            daily = result.get("dailyReportList")
            sum_report = result.get("sumReport")
            if (
                not isinstance(daily, list)
                or not daily
                or not isinstance(daily[0], dict)
                or not isinstance(sum_report, dict)
            ):
                raise RuntimeError("account report metric containers are invalid")
            daily_spend = _yuan_cents(daily[0].get("spend"))
            summary_spend = _yuan_cents(sum_report.get("spend"))
            if daily_spend != summary_spend:
                raise RuntimeError("account daily and summary spend disagree")
            spend_by_date[request_business_date] = summary_spend
            selected_cutoff = (
                str(page_time_scope["today_cutoff_hhmm"])
                if window_kind == "TODAY"
                else str(page_time_scope["yesterday_cutoff_hhmm"])
            )
            cutoff_hour = int(selected_cutoff[:2])
            end_day_hour = request_summary["end_day_hour"]
            if type(end_day_hour) is not int:
                raise RuntimeError("account report endDayHour is not an integer")
            if (
                end_day_hour > capture_after.hour
                or (window_kind == "YESTERDAY" and end_day_hour != cutoff_hour)
                or (window_kind == "TODAY" and end_day_hour < cutoff_hour)
            ):
                raise RuntimeError("account request hour and page cutoff disagree")
            source_update = response_summary["source_update_timestamp"]
            if window_kind == "TODAY":
                if not isinstance(source_update, str):
                    raise RuntimeError("account TODAY source update is unavailable")
                source_update_local = datetime.fromisoformat(source_update).astimezone(ZONE)
                if (
                    source_update_local.date() != capture_before.date()
                    or source_update_local > capture_after
                    or source_update_local.strftime("%H:%M") != selected_cutoff
                ):
                    raise RuntimeError("account TODAY source update and page cutoff disagree")
            summaries.append(
                {
                    "window_kind": window_kind,
                    "identity_entity_matches_expected": True,
                    "request": request_summary,
                    "response": response_summary,
                }
            )
        if observed_windows != {"TODAY", "YESTERDAY"}:
            raise RuntimeError("account TODAY/YESTERDAY response pair is incomplete")
        summaries.sort(key=lambda item: 0 if item["window_kind"] == "TODAY" else 1)
        indicator_texts = await page.locator(
            "div[class*='ChartBlockArea_indicator__']"
        ).all_text_contents()
        total_spend_indicators = [
            text for text in indicator_texts if "总花费(元)" in "".join(text.split())
        ]
        if len(total_spend_indicators) != 1:
            raise RuntimeError("account total-spend DOM indicator is not unique")
        dom_numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", total_spend_indicators[0])
        try:
            dom_spend_cents = [int(Decimal(value.replace(",", "")) * 100) for value in dom_numbers]
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError("account total-spend DOM values are invalid") from exc
        dates = sorted(spend_by_date)
        if len(dates) != 2 or len(dom_spend_cents) != 2:
            raise RuntimeError("account TODAY/YESTERDAY DOM crosscheck is incomplete")
        today_position_matches = dom_spend_cents[0] == spend_by_date[dates[-1]]
        yesterday_position_matches = dom_spend_cents[1] == spend_by_date[dates[0]]
        if not today_position_matches or not yesterday_position_matches:
            raise RuntimeError("account TODAY/YESTERDAY DOM values do not match their responses")
        dom_crosscheck = {
            "today_position_matches": True,
            "yesterday_position_matches": True,
            "date_explanation_unique": True,
            "values_printed": False,
        }
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "identity_matches_expected": True,
                    "page_time_scope": page_time_scope,
                    "response_count": len(summaries),
                    "reports": summaries,
                    "dom_crosscheck": dom_crosscheck,
                    "metric_values_printed": False,
                    "raw_ids_printed": False,
                    "raw_bodies_persisted": False,
                    "browser_closed": False,
                    "error_tab_restored": error_tab_restored,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    finally:
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only account/day promotion semantics.")
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--restore-error-tab", action="store_true")
    parser.add_argument("--confirm-read-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256):
        parser.error("--expected-sha256 must be a lowercase SHA-256 digest")
    if args.restore_error_tab and not args.confirm_read_only:
        parser.error("--confirm-read-only is required with --restore-error-tab")
    asyncio.run(inspect(args.expected_sha256, restore_error_tab=args.restore_error_tab))


if __name__ == "__main__":
    main()
