from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from pdd_data_mcp.contracts.models import InternalId, StrictModel


class ServiceSettings(StrictModel):
    name: str = "pdd-data-mcp"
    transport: str = "stdio"
    mode: str = "read_only"
    test_mode: bool = False

    @model_validator(mode="after")
    def validate_fixed_values(self) -> ServiceSettings:
        if self.name != "pdd-data-mcp" or self.transport != "stdio" or self.mode != "read_only":
            raise ValueError("service name, transport, and mode are fixed for V0.1")
        return self


class StorageSettings(StrictModel):
    backend: str = "local_files"
    data_root: Path
    runtime_root: Path
    partition_timezone: str = "Asia/Shanghai"
    layout_version: str = "1"
    max_bytes: int = Field(default=10_737_418_240, ge=1)
    min_free_bytes: int = Field(default=1_073_741_824, ge=0)
    retain_committed_snapshots: bool = True
    raw_response_persistence: bool = False

    @model_validator(mode="after")
    def validate_storage(self) -> StorageSettings:
        if self.backend != "local_files":
            raise ValueError("only local_files storage is supported")
        if not self.retain_committed_snapshots:
            raise ValueError("automatic committed snapshot deletion is not supported")
        if self.raw_response_persistence:
            raise ValueError("raw response persistence is forbidden")
        data = self.data_root.resolve(strict=False)
        runtime = self.runtime_root.resolve(strict=False)
        if data == runtime or data in runtime.parents or runtime in data.parents:
            raise ValueError("data_root and runtime_root must not overlap")
        return self


class CollectionSettings(StrictModel):
    max_concurrency: int = Field(default=1, ge=1, le=1)
    max_products: int = Field(default=50, ge=1, le=200)
    connect_timeout_ms: int = Field(default=10_000, ge=100)
    collection_timeout_ms: int = Field(default=90_000, ge=100)
    min_interval_seconds: int = Field(default=60, ge=0)
    platform_auto_retries: int = Field(default=0, ge=0, le=0)
    max_mcp_response_bytes: int = Field(default=262_144, ge=4096)
    max_browser_response_bytes: int = Field(default=1_048_576, ge=4096, le=8_388_608)
    max_inflight_responses: int = Field(default=4, ge=1, le=16)


class DiscoverySettings(StrictModel):
    enabled: bool = False
    observe_seconds: int = Field(default=20, ge=5, le=120)
    max_metadata_entries: int = Field(default=100, ge=1, le=500)
    candidate_body_probe_enabled: bool = False
    candidate_response_host: str = Field(default="", max_length=253)
    candidate_response_paths: list[str] = Field(default_factory=list, max_length=4)
    candidate_response_method: Literal["GET", "POST"] = "POST"

    @model_validator(mode="after")
    def validate_candidate_probe(self) -> DiscoverySettings:
        if not self.candidate_body_probe_enabled:
            return self
        if not self.candidate_response_host or not self.candidate_response_paths:
            raise ValueError("candidate body probe requires an exact host and at least one path")
        if len(set(self.candidate_response_paths)) != len(self.candidate_response_paths):
            raise ValueError("candidate response paths must be unique")
        if any(not path.startswith("/") or "?" in path for path in self.candidate_response_paths):
            raise ValueError("candidate response paths must be exact query-free absolute paths")
        return self


class PromotionAdapterSettings(StrictModel):
    verified: bool = False
    response_host: str = Field(default="", max_length=253)
    response_path: str = Field(default="", max_length=512)
    response_method: Literal["GET", "POST"] = "GET"
    response_http_status: int = Field(default=200, ge=100, le=599)
    response_content_type: Literal["application/json"] = "application/json"
    business_success_path: str = Field(default="", max_length=256)
    business_success_value: bool | int | str = True
    platform_store_id_path: str = Field(default="", max_length=256)
    identity_response_host: str = Field(default="", max_length=253)
    identity_response_path: str = Field(default="", max_length=512)
    identity_response_method: Literal["GET", "POST"] = "POST"
    identity_response_http_status: int = Field(default=200, ge=100, le=599)
    identity_business_success_path: str = Field(default="", max_length=256)
    identity_business_success_value: bool | int | str = True
    identity_platform_store_id_path: str = Field(default="", max_length=256)
    identity_verification_method: Literal["CONFIG_EXACT_ID", "MERCHANT_PAGE_STATE_SHA256"] = (
        "CONFIG_EXACT_ID"
    )
    identity_verification_reference: str = Field(default="", max_length=128)
    metric_list_path: str = Field(default="", max_length=256)
    metric_item_business_date_path: str = Field(default="", max_length=256)
    metric_item_date_format: Literal["ISO_DATE", "ISO_DATETIME_SECONDS"] = "ISO_DATE"
    business_date_path: str = Field(default="", max_length=256)
    ad_spend_path: str = Field(default="", max_length=256)
    ad_spend_unit: Literal["CNY", "CNY_CENT"] = "CNY_CENT"
    ad_spend_unit_path: str = Field(default="", max_length=256)
    ad_spend_expected_unit_value: str = Field(default="", max_length=64)
    source_updated_at_path: str = Field(default="", max_length=256)
    parser_version: str = Field(default="", max_length=64)
    trigger: Literal["MANUAL", "RELOAD"] = "MANUAL"
    dom_fallback_enabled: bool = False
    dom_store_id_selector: str = Field(default="", max_length=512)
    dom_store_id_attribute: str = Field(default="", max_length=128)
    dom_business_date_selector: str = Field(default="", max_length=512)
    dom_business_date_attribute: str = Field(default="", max_length=128)
    dom_today_label: str = Field(default="", max_length=64)
    dom_ad_spend_selector: str = Field(default="", max_length=512)
    dom_ad_spend_attribute: str = Field(default="", max_length=128)
    dom_ad_spend_unit: Literal["CNY", "CNY_CENT"] = "CNY"
    login_selector: str = Field(default="", max_length=512)
    captcha_selector: str = Field(default="", max_length=512)
    error_selector: str = Field(default="", max_length=512)

    @field_validator(
        "business_success_path",
        "platform_store_id_path",
        "identity_business_success_path",
        "identity_platform_store_id_path",
        "metric_list_path",
        "metric_item_business_date_path",
        "business_date_path",
        "ad_spend_path",
        "ad_spend_unit_path",
        "source_updated_at_path",
    )
    @classmethod
    def validate_json_path(cls, value: str) -> str:
        if not value:
            return value
        parts = value.split(".")
        if any(not part.replace("_", "").isalnum() or not part[0].isalpha() for part in parts):
            raise ValueError("adapter JSON paths must be simple dotted object keys")
        return value

    @model_validator(mode="after")
    def validate_verified_adapter(self) -> PromotionAdapterSettings:
        if not self.verified:
            return self
        required = {
            "response_host": self.response_host,
            "response_path": self.response_path,
            "business_success_path": self.business_success_path,
            "ad_spend_path": self.ad_spend_path,
            "parser_version": self.parser_version,
            "dom_business_date_selector": self.dom_business_date_selector,
            "dom_ad_spend_selector": self.dom_ad_spend_selector,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if self.identity_response_path:
            identity_required = {
                "identity_response_host": self.identity_response_host,
                "identity_business_success_path": self.identity_business_success_path,
                "identity_platform_store_id_path": self.identity_platform_store_id_path,
            }
            missing.extend(name for name, value in identity_required.items() if not value)
            if (
                not self.identity_response_path.startswith("/")
                or "?" in self.identity_response_path
            ):
                raise ValueError("identity_response_path must be exact and query-free")
        elif not self.platform_store_id_path:
            missing.append("platform_store_id_path")
        if self.metric_list_path:
            if not self.metric_item_business_date_path:
                missing.append("metric_item_business_date_path")
        elif not self.business_date_path:
            missing.append("business_date_path")
        if bool(self.ad_spend_unit_path) != bool(self.ad_spend_expected_unit_value):
            raise ValueError(
                "response money unit path and expected value must be configured together"
            )
        if (
            self.identity_verification_method == "MERCHANT_PAGE_STATE_SHA256"
            and not self.identity_verification_reference
        ):
            missing.append("identity_verification_reference")
        missing = sorted(set(missing))
        if missing:
            raise ValueError(f"verified promotion adapter is missing: {', '.join(missing)}")
        if not self.response_path.startswith("/") or "?" in self.response_path:
            raise ValueError("response_path must be an exact query-free absolute path")
        if self.dom_fallback_enabled and not self.dom_store_id_selector:
            raise ValueError("DOM fallback requires a verified platform store ID selector")
        return self


class CoreAdapterBaseSettings(StrictModel):
    verified: bool = False
    data_source: Literal["NETWORK_RESPONSE", "DOM"] = "NETWORK_RESPONSE"
    target_page_url: str = ""
    response_host: str = Field(default="", max_length=253)
    response_path: str = Field(default="", max_length=512)
    response_method: Literal["GET", "POST"] = "POST"
    response_http_status: int = Field(default=200, ge=100, le=599)
    business_success_path: str = Field(default="", max_length=256)
    business_success_value: bool | int | str = True
    identity_source: Literal["MAIN_RESPONSE", "IDENTITY_RESPONSE", "MERCHANT_PAGE_STATE_SHA256"] = (
        "MAIN_RESPONSE"
    )
    platform_store_id_path: str = Field(default="", max_length=256)
    identity_response_host: str = Field(default="", max_length=253)
    identity_response_path: str = Field(default="", max_length=512)
    identity_response_method: Literal["GET", "POST"] = "POST"
    identity_response_http_status: int = Field(default=200, ge=100, le=599)
    identity_business_success_path: str = Field(default="", max_length=256)
    identity_business_success_value: bool | int | str = True
    identity_platform_store_id_path: str = Field(default="", max_length=256)
    identity_page_url: str = ""
    identity_verification_reference: str = Field(default="", max_length=128)
    parser_version: str = Field(default="", max_length=64)
    trigger: Literal["MANUAL", "RELOAD"] = "MANUAL"
    login_selector: str = Field(default="", max_length=512)
    captcha_selector: str = Field(default="", max_length=512)
    error_selector: str = Field(default="", max_length=512)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)

    @field_validator(
        "business_success_path",
        "platform_store_id_path",
        "identity_business_success_path",
        "identity_platform_store_id_path",
    )
    @classmethod
    def validate_core_json_path(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @model_validator(mode="after")
    def validate_verified_core_base(self) -> CoreAdapterBaseSettings:
        if not self.verified:
            return self
        required = {
            "target_page_url": self.target_page_url,
            "parser_version": self.parser_version,
            "identity_verification_reference": self.identity_verification_reference,
        }
        if self.data_source == "NETWORK_RESPONSE":
            required.update(
                {
                    "response_host": self.response_host,
                    "response_path": self.response_path,
                    "business_success_path": self.business_success_path,
                }
            )
        if self.identity_source == "MAIN_RESPONSE":
            required["platform_store_id_path"] = self.platform_store_id_path
        elif self.identity_source == "IDENTITY_RESPONSE":
            required.update(
                {
                    "identity_response_host": self.identity_response_host,
                    "identity_response_path": self.identity_response_path,
                    "identity_business_success_path": self.identity_business_success_path,
                    "identity_platform_store_id_path": self.identity_platform_store_id_path,
                }
            )
        else:
            required["identity_page_url"] = self.identity_page_url
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise ValueError(f"verified core adapter is missing: {', '.join(missing)}")
        for name, value in {
            "response_path": self.response_path,
            "identity_response_path": self.identity_response_path,
        }.items():
            if value and (not value.startswith("/") or "?" in value):
                raise ValueError(f"{name} must be exact and query-free")
        return self


class StoreMetricSettings(StrictModel):
    response_path: str = Field(default="", max_length=256)
    source_unit: Literal["CNY", "CNY_CENT", "COUNT", "RATIO", "PERCENT"]
    output_unit: Literal["CNY_CENT", "COUNT", "RATIO"]
    response_unit_path: str = Field(default="", max_length=256)
    expected_response_unit: str = Field(default="", max_length=64)
    precision: Literal["EXACT", "APPROXIMATE"] = "EXACT"
    dom_selector: str = Field(default="", max_length=512)
    dom_attribute: str = Field(default="", max_length=128)

    @field_validator("response_path", "response_unit_path")
    @classmethod
    def validate_metric_json_path(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @model_validator(mode="after")
    def validate_metric_mapping(self) -> StoreMetricSettings:
        if not self.response_path and not self.dom_selector:
            raise ValueError("store metric requires a response path or DOM selector")
        if bool(self.response_unit_path) != bool(self.expected_response_unit):
            raise ValueError("metric response unit path and expected value must be paired")
        return self


PromotionWindowName = Literal[
    "TODAY",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_30_DAYS",
    "LAST_90_DAYS",
]


PromotionAccountWindowName = Literal["TODAY", "YESTERDAY"]


class PromotionAccountAdapterSettings(CoreAdapterBaseSettings):
    """Evidence-bound account/day promotion report adapter."""

    response_content_type: Literal["application/json"] = "application/json"
    request_contract_version: Literal["", "PROMOTION_ACCOUNT_HOURLY_DUAL_V1"] = ""
    supported_windows: list[PromotionAccountWindowName] = Field(default_factory=list, max_length=2)
    daily_report_list_path: str = Field(default="", max_length=256)
    summary_path: str = Field(default="", max_length=256)
    daily_business_date_path: str = Field(default="", max_length=256)
    request_date_format: Literal["", "PDD_MIDNIGHT_SECONDS"] = ""
    response_date_format: Literal["", "PDD_MIDNIGHT_SECONDS"] = ""
    result_source_updated_at_path: str = Field(default="", max_length=256)
    secondary_source_updated_at_path: str = Field(default="", max_length=256)
    request_entity_id_field: str = Field(default="", max_length=64)
    request_start_date_field: str = Field(default="", max_length=64)
    request_end_date_field: str = Field(default="", max_length=64)
    request_query_dimension_type_field: str = Field(default="", max_length=64)
    request_report_promotion_type_field: str = Field(default="", max_length=64)
    request_client_type_field: str = Field(default="", max_length=64)
    request_end_day_hour_field: str = Field(default="", max_length=64)
    request_return_last_update_time_field: str = Field(default="", max_length=64)
    request_block_types_field: str = Field(default="", max_length=64)
    request_crawler_info_field: str = Field(default="", max_length=64)
    request_crawler_info_max_length: int = Field(default=4096, ge=1, le=16_384)
    request_end_day_hour_semantics: Literal["INCLUSIVE_HOURLY_ROW_INDEX_WITH_DOM_CUTOFF"] = (
        "INCLUSIVE_HOURLY_ROW_INDEX_WITH_DOM_CUTOFF"
    )
    dom_today_spend_selector: str = Field(default="", max_length=512)
    dom_yesterday_spend_selector: str = Field(default="", max_length=512)
    dom_report_date_explanation_selector: str = Field(default="", max_length=512)
    dom_spend_unit: Literal["CNY"] = "CNY"

    @field_validator(
        "daily_report_list_path",
        "summary_path",
        "daily_business_date_path",
        "result_source_updated_at_path",
        "secondary_source_updated_at_path",
        "request_entity_id_field",
        "request_start_date_field",
        "request_end_date_field",
        "request_query_dimension_type_field",
        "request_report_promotion_type_field",
        "request_client_type_field",
        "request_end_day_hour_field",
        "request_return_last_update_time_field",
        "request_block_types_field",
        "request_crawler_info_field",
    )
    @classmethod
    def validate_promotion_account_path(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @model_validator(mode="after")
    def validate_verified_promotion_account(self) -> PromotionAccountAdapterSettings:
        if not self.verified:
            return self
        if self.data_source != "NETWORK_RESPONSE":
            raise ValueError("promotion account metrics require a verified network response")
        if self.request_contract_version != "PROMOTION_ACCOUNT_HOURLY_DUAL_V1":
            raise ValueError("verified promotion account adapter requires its request contract")
        if self.parser_version != "d4-account-v3":
            raise ValueError("verified promotion account adapter requires parser d4-account-v3")
        if (
            self.request_date_format != "PDD_MIDNIGHT_SECONDS"
            or self.response_date_format != "PDD_MIDNIGHT_SECONDS"
        ):
            raise ValueError("verified promotion account adapter requires midnight-second dates")
        evidenced_transport = {
            "target_page_url": (
                self.target_page_url,
                "https://yingxiao.pinduoduo.com/mains/promotionOverview",
            ),
            "response_host": (self.response_host, "yingxiao.pinduoduo.com"),
            "response_path": (
                self.response_path,
                "/mms-gateway/poseidon/api/report/queryHourlyRangeReport",
            ),
            "response_method": (self.response_method, "POST"),
            "response_http_status": (self.response_http_status, 200),
            "response_content_type": (self.response_content_type, "application/json"),
            "trigger": (self.trigger, "RELOAD"),
            "identity_source": (self.identity_source, "IDENTITY_RESPONSE"),
            "identity_response_host": (
                self.identity_response_host,
                "yingxiao.pinduoduo.com",
            ),
            "identity_response_path": (
                self.identity_response_path,
                "/mms-gateway/venus/api/user/info",
            ),
            "identity_response_method": (self.identity_response_method, "POST"),
            "identity_response_http_status": (self.identity_response_http_status, 200),
            "identity_business_success_path": (
                self.identity_business_success_path,
                "success",
            ),
            "identity_platform_store_id_path": (
                self.identity_platform_store_id_path,
                "result.mallId",
            ),
            "dom_report_date_explanation_selector": (
                self.dom_report_date_explanation_selector,
                "div[class*='ReportDateExplain_content__']",
            ),
        }
        changed_transport = sorted(
            name
            for name, (actual, expected) in evidenced_transport.items()
            if type(actual) is not type(expected) or actual != expected
        )
        if changed_transport:
            raise ValueError(
                "verified promotion account transport changed: " + ", ".join(changed_transport)
            )
        if type(self.business_success_value) is not bool or self.business_success_value is not True:
            raise ValueError("verified promotion account adapter requires exact success=true")
        if (
            type(self.identity_business_success_value) is not bool
            or self.identity_business_success_value is not True
        ):
            raise ValueError("verified promotion account identity requires exact success=true")

        evidenced_values = {
            "business_success_path": (self.business_success_path, "success"),
            "daily_report_list_path": (self.daily_report_list_path, "result.dailyReportList"),
            "summary_path": (self.summary_path, "result.sumReport"),
            "daily_business_date_path": (self.daily_business_date_path, "date"),
            "result_source_updated_at_path": (
                self.result_source_updated_at_path,
                "result.reportLastUpdateTime",
            ),
            "secondary_source_updated_at_path": (
                self.secondary_source_updated_at_path,
                "result.lastUpdateTime",
            ),
            "request_entity_id_field": (self.request_entity_id_field, "entityId"),
            "request_start_date_field": (self.request_start_date_field, "startDate"),
            "request_end_date_field": (self.request_end_date_field, "endDate"),
            "request_query_dimension_type_field": (
                self.request_query_dimension_type_field,
                "queryDimensionType",
            ),
            "request_report_promotion_type_field": (
                self.request_report_promotion_type_field,
                "reportPromotionType",
            ),
            "request_client_type_field": (self.request_client_type_field, "clientType"),
            "request_end_day_hour_field": (self.request_end_day_hour_field, "endDayHour"),
            "request_return_last_update_time_field": (
                self.request_return_last_update_time_field,
                "returnLastUpdateTime",
            ),
            "request_block_types_field": (self.request_block_types_field, "blockTypes"),
            "request_crawler_info_field": (self.request_crawler_info_field, "crawlerInfo"),
        }
        changed = sorted(
            name for name, (actual, expected) in evidenced_values.items() if actual != expected
        )
        if changed:
            raise ValueError(
                "verified promotion account evidence fields changed: " + ", ".join(changed)
            )
        if len(self.supported_windows) != 2 or set(self.supported_windows) != {
            "TODAY",
            "YESTERDAY",
        }:
            raise ValueError("verified promotion account adapter supports only TODAY and YESTERDAY")
        if not self.dom_today_spend_selector or not self.dom_yesterday_spend_selector:
            raise ValueError("verified promotion account adapter requires both DOM spend selectors")
        if self.dom_today_spend_selector == self.dom_yesterday_spend_selector:
            raise ValueError("TODAY and YESTERDAY DOM spend selectors must be different")
        return self


class PromotionMetricsAdapterSettings(CoreAdapterBaseSettings):
    """Evidence-bound promoted-product metrics and current configuration adapter."""

    request_contract_version: Literal["", "PROMOTED_PRODUCT_LIST_UNFILTERED_V1"] = ""
    request_crawler_info_max_length: int = Field(default=4096, ge=1, le=16_384)
    supported_windows: list[PromotionWindowName] = Field(default_factory=list, max_length=5)
    quick_option_testids: dict[PromotionWindowName, str] = Field(default_factory=dict, max_length=5)
    list_path: str = Field(default="", max_length=256)
    summary_path: str = Field(default="", max_length=256)
    source_updated_at_path: str = Field(default="", max_length=256)
    row_platform_store_id_path: str = Field(default="", max_length=256)
    promotion_id_path: str = Field(default="", max_length=256)
    campaign_id_path: str = Field(default="", max_length=256)
    platform_product_id_path: str = Field(default="", max_length=256)
    product_name_path: str = Field(default="", max_length=256)
    report_path: str = Field(default="", max_length=256)
    max_cost_path: str = Field(default="", max_length=256)
    target_roi_path: str = Field(default="", max_length=256)
    agent_bid_path: str = Field(default="", max_length=256)
    ad_status_path: str = Field(default="", max_length=256)
    request_begin_date_field: str = Field(default="", max_length=64)
    request_end_date_field: str = Field(default="", max_length=64)
    request_page_number_field: str = Field(default="", max_length=64)
    request_page_size_field: str = Field(default="", max_length=64)
    platform_page_size: int = Field(default=50, ge=1, le=200)
    pagination_terminal_rule: Literal["SHORT_PAGE"] = "SHORT_PAGE"
    dom_row_selector: str = Field(default="", max_length=512)
    reload_resets_to_today: bool = False
    configuration_current_only: bool = True

    @field_validator(
        "list_path",
        "summary_path",
        "source_updated_at_path",
        "row_platform_store_id_path",
        "promotion_id_path",
        "campaign_id_path",
        "platform_product_id_path",
        "product_name_path",
        "report_path",
        "max_cost_path",
        "target_roi_path",
        "agent_bid_path",
        "ad_status_path",
        "request_begin_date_field",
        "request_end_date_field",
        "request_page_number_field",
        "request_page_size_field",
    )
    @classmethod
    def validate_promotion_metrics_path(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @field_validator("quick_option_testids")
    @classmethod
    def validate_quick_option_testids(
        cls, value: dict[PromotionWindowName, str]
    ) -> dict[PromotionWindowName, str]:
        if len(set(value.values())) != len(value):
            raise ValueError("promotion window test IDs must be unique")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", item) for item in value.values()):
            raise ValueError("promotion window test IDs must be fixed safe identifiers")
        return value

    @model_validator(mode="after")
    def validate_verified_promotion_metrics(self) -> PromotionMetricsAdapterSettings:
        if not self.verified:
            return self
        if self.data_source != "NETWORK_RESPONSE":
            raise ValueError("promotion metrics require a verified network response")
        if self.request_contract_version != "PROMOTED_PRODUCT_LIST_UNFILTERED_V1":
            raise ValueError("verified promotion metrics require the evidenced request contract")
        if self.supported_windows != ["TODAY"]:
            raise ValueError("verified promoted-product metrics support only evidenced TODAY")
        if self.quick_option_testids != {"TODAY": "DateAreaQuickOption_0"}:
            raise ValueError(
                "verified promoted-product metrics require the evidenced TODAY test ID"
            )
        if self.platform_page_size != 50:
            raise ValueError("verified promoted-product metrics require evidenced page size 50")
        evidenced_transport = {
            "target_page_url": (
                self.target_page_url,
                "https://yingxiao.pinduoduo.com/goods/promotion/list",
            ),
            "response_host": (self.response_host, "yingxiao.pinduoduo.com"),
            "response_path": (
                self.response_path,
                "/mms-gateway/venus/api/goods/promotion/v3/list",
            ),
            "response_method": (self.response_method, "POST"),
            "response_http_status": (self.response_http_status, 200),
            "business_success_path": (self.business_success_path, "success"),
            "business_success_value": (self.business_success_value, True),
            "identity_source": (self.identity_source, "IDENTITY_RESPONSE"),
            "identity_response_host": (
                self.identity_response_host,
                "yingxiao.pinduoduo.com",
            ),
            "identity_response_path": (
                self.identity_response_path,
                "/mms-gateway/venus/api/user/userInfo",
            ),
            "identity_response_method": (self.identity_response_method, "POST"),
            "identity_response_http_status": (self.identity_response_http_status, 200),
            "identity_business_success_path": (
                self.identity_business_success_path,
                "success",
            ),
            "identity_business_success_value": (
                self.identity_business_success_value,
                True,
            ),
            "identity_platform_store_id_path": (
                self.identity_platform_store_id_path,
                "result.mall.mallId",
            ),
            "parser_version": (
                self.parser_version,
                "pdd-promoted-product-v3/1.0.0",
            ),
            "trigger": (self.trigger, "RELOAD"),
            "dom_row_selector": (self.dom_row_selector, "tbody tr"),
        }
        changed_transport = sorted(
            name
            for name, (actual, expected) in evidenced_transport.items()
            if type(actual) is not type(expected) or actual != expected
        )
        if changed_transport:
            raise ValueError(
                "verified promoted-product transport changed: " + ", ".join(changed_transport)
            )
        evidenced_paths = {
            "list_path": "result.adInfos",
            "summary_path": "result.sumReportInfo",
            "source_updated_at_path": "result.reportLastUpdateTime",
            "row_platform_store_id_path": "mallId",
            "promotion_id_path": "adId",
            "campaign_id_path": "planId",
            "platform_product_id_path": "goodsId",
            "product_name_path": "goodsInfo.goodsName",
            "report_path": "reportInfo",
            "max_cost_path": "maxCost",
            "target_roi_path": "targetRoi",
            "agent_bid_path": "agentBid",
            "ad_status_path": "adStatus",
            "request_begin_date_field": "beginDate",
            "request_end_date_field": "endDate",
            "request_page_number_field": "pageNumber",
            "request_page_size_field": "pageSize",
        }
        changed_paths = sorted(
            field for field, expected in evidenced_paths.items() if getattr(self, field) != expected
        )
        if changed_paths:
            raise ValueError(
                "verified promoted-product evidence paths changed: " + ", ".join(changed_paths)
            )
        required = {
            "list_path": self.list_path,
            "summary_path": self.summary_path,
            "source_updated_at_path": self.source_updated_at_path,
            "row_platform_store_id_path": self.row_platform_store_id_path,
            "promotion_id_path": self.promotion_id_path,
            "campaign_id_path": self.campaign_id_path,
            "platform_product_id_path": self.platform_product_id_path,
            "product_name_path": self.product_name_path,
            "report_path": self.report_path,
            "max_cost_path": self.max_cost_path,
            "target_roi_path": self.target_roi_path,
            "agent_bid_path": self.agent_bid_path,
            "ad_status_path": self.ad_status_path,
            "request_begin_date_field": self.request_begin_date_field,
            "request_end_date_field": self.request_end_date_field,
            "request_page_number_field": self.request_page_number_field,
            "request_page_size_field": self.request_page_size_field,
            "dom_row_selector": self.dom_row_selector,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise ValueError(f"verified promotion metrics adapter is missing: {', '.join(missing)}")
        if not self.supported_windows:
            raise ValueError("verified promotion metrics adapter requires supported windows")
        if set(self.quick_option_testids) != set(self.supported_windows):
            raise ValueError("every supported promotion window requires one exact test ID")
        if not self.reload_resets_to_today:
            raise ValueError("verified adapter requires an evidenced reload-to-TODAY gate")
        if not self.configuration_current_only:
            raise ValueError("promotion configuration must remain current-only")
        return self


class StoreOverviewAdapterSettings(CoreAdapterBaseSettings):
    business_date_path: str = Field(default="", max_length=256)
    business_date_format: Literal["ISO_DATE", "ISO_DATETIME_SECONDS"] = "ISO_DATE"
    metrics: dict[str, StoreMetricSettings] = Field(default_factory=dict, max_length=16)
    dom_business_date_selector: str = Field(default="", max_length=512)
    dom_business_date_attribute: str = Field(default="", max_length=128)
    dom_today_label: str = Field(default="", max_length=64)
    dom_business_date_format: Literal["EXACT_LABEL", "CONTAINS_ISO_DATETIME_SECONDS"] = (
        "EXACT_LABEL"
    )

    @field_validator("business_date_path")
    @classmethod
    def validate_store_date_path(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @model_validator(mode="after")
    def validate_verified_store(self) -> StoreOverviewAdapterSettings:
        if self.verified:
            if not self.dom_business_date_selector:
                raise ValueError("verified store overview requires DOM time evidence")
            if self.data_source == "NETWORK_RESPONSE" and not self.business_date_path:
                raise ValueError("network store overview requires a response date")
            if self.data_source == "DOM" and any(
                mapping.response_path for mapping in self.metrics.values()
            ):
                raise ValueError("DOM store overview cannot declare network metric paths")
            if len(self.metrics) < 2:
                raise ValueError("verified store overview requires at least two metric mappings")
            if self.data_source == "DOM" and any(
                not mapping.dom_selector for mapping in self.metrics.values()
            ):
                raise ValueError("DOM store overview requires a selector for every metric")
        return self


class ProductCatalogAdapterSettings(CoreAdapterBaseSettings):
    list_path: str = Field(default="", max_length=256)
    total_path: str = Field(default="", max_length=256)
    product_id_path: str = Field(default="", max_length=256)
    item_platform_store_id_path: str = Field(default="", max_length=256)
    product_name_path: str = Field(default="", max_length=256)
    status_path: str = Field(default="", max_length=256)
    status_map: dict[str, Literal["ON_SALE", "OFF_SALE", "UNKNOWN"]] = Field(
        default_factory=dict, max_length=32
    )
    price_path: str = Field(default="", max_length=256)
    price_unit: Literal["CNY", "CNY_CENT"] = "CNY_CENT"
    sku_count_path: str = Field(default="", max_length=256)
    created_at_path: str = Field(default="", max_length=256)
    published_at_path: str = Field(default="", max_length=256)
    dom_total_selector: str = Field(default="", max_length=512)
    dom_total_attribute: str = Field(default="", max_length=128)
    next_page_selector: str = Field(default="", max_length=512)

    @field_validator(
        "list_path",
        "total_path",
        "product_id_path",
        "item_platform_store_id_path",
        "product_name_path",
        "status_path",
        "price_path",
        "sku_count_path",
        "created_at_path",
        "published_at_path",
    )
    @classmethod
    def validate_product_paths(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @model_validator(mode="after")
    def validate_verified_product(self) -> ProductCatalogAdapterSettings:
        if self.verified:
            if self.data_source != "NETWORK_RESPONSE":
                raise ValueError("product catalog requires a verified network response")
            required = {
                "list_path": self.list_path,
                "total_path": self.total_path,
                "product_id_path": self.product_id_path,
                "dom_total_selector": self.dom_total_selector,
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(f"verified product adapter is missing: {', '.join(missing)}")
        return self


class InventoryAdapterSettings(CoreAdapterBaseSettings):
    list_path: str = Field(default="", max_length=256)
    total_path: str = Field(default="", max_length=256)
    product_id_path: str = Field(default="", max_length=256)
    item_platform_store_id_path: str = Field(default="", max_length=256)
    inventory_path: str = Field(default="", max_length=256)
    granularity: Literal["PRODUCT", "SKU"] = "PRODUCT"
    sku_list_path: str = Field(default="", max_length=256)
    sku_id_path: str = Field(default="", max_length=256)
    dom_total_selector: str = Field(default="", max_length=512)
    dom_total_attribute: str = Field(default="", max_length=128)
    next_page_selector: str = Field(default="", max_length=512)

    @field_validator(
        "list_path",
        "total_path",
        "product_id_path",
        "item_platform_store_id_path",
        "inventory_path",
        "sku_list_path",
        "sku_id_path",
    )
    @classmethod
    def validate_inventory_paths(cls, value: str) -> str:
        return PromotionAdapterSettings.validate_json_path(value)

    @model_validator(mode="after")
    def validate_verified_inventory(self) -> InventoryAdapterSettings:
        if self.verified:
            if self.data_source != "NETWORK_RESPONSE":
                raise ValueError("inventory requires a verified network response")
            required = {
                "list_path": self.list_path,
                "total_path": self.total_path,
                "product_id_path": self.product_id_path,
                "inventory_path": self.inventory_path,
                "dom_total_selector": self.dom_total_selector,
            }
            if self.granularity == "SKU":
                required["sku_list_path"] = self.sku_list_path
                required["sku_id_path"] = self.sku_id_path
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(f"verified inventory adapter is missing: {', '.join(missing)}")
        return self


class ConnectionSettings(StrictModel):
    connection_id: InternalId
    store_id: InternalId
    cdp_endpoint: str | None = None
    expected_platform_store_id: str = ""
    expected_platform_store_id_sha256: str = Field(default="", pattern=r"^[0-9a-f]{64}$|^$")
    target_page_url: str = "https://yingxiao.pinduoduo.com/mains/promotionOverview"
    real_collection_enabled: bool = False
    synthetic_enabled: bool = False
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    promotion_adapter: PromotionAdapterSettings = Field(default_factory=PromotionAdapterSettings)
    promotion_account_adapter: PromotionAccountAdapterSettings = Field(
        default_factory=PromotionAccountAdapterSettings
    )
    promotion_metrics_adapter: PromotionMetricsAdapterSettings = Field(
        default_factory=PromotionMetricsAdapterSettings
    )
    store_overview_adapter: StoreOverviewAdapterSettings = Field(
        default_factory=StoreOverviewAdapterSettings
    )
    product_catalog_adapter: ProductCatalogAdapterSettings = Field(
        default_factory=ProductCatalogAdapterSettings
    )
    inventory_adapter: InventoryAdapterSettings = Field(default_factory=InventoryAdapterSettings)

    @field_validator("cdp_endpoint", mode="before")
    @classmethod
    def normalize_cdp_endpoint(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("cdp_endpoint")
    @classmethod
    def validate_cdp_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "ws"}:
            raise ValueError("CDP endpoint must use http or ws")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CDP endpoint must use an explicit loopback host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("CDP endpoint must not contain credentials, query, or fragment")
        if parsed.port is None:
            raise ValueError("CDP endpoint requires an explicit port")
        return value

    @field_validator("target_page_url")
    @classmethod
    def validate_target_page_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target_page_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("target_page_url must not contain credentials or fragment")
        return value

    @model_validator(mode="after")
    def validate_real_collection(self) -> ConnectionSettings:
        if self.real_collection_enabled:
            if self.synthetic_enabled:
                raise ValueError("real and synthetic collection cannot share one connection")
            if self.cdp_endpoint is None:
                raise ValueError("real collection requires a loopback cdp_endpoint")
            identity_values = bool(self.expected_platform_store_id) + bool(
                self.expected_platform_store_id_sha256
            )
            if identity_values != 1:
                raise ValueError(
                    "real collection requires exactly one expected platform store identity"
                )
            core_adapters = (
                self.promotion_account_adapter,
                self.promotion_metrics_adapter,
                self.store_overview_adapter,
                self.product_catalog_adapter,
                self.inventory_adapter,
            )
            if not (
                self.promotion_adapter.verified
                or self.discovery.enabled
                or any(adapter.verified or adapter.discovery.enabled for adapter in core_adapters)
            ):
                raise ValueError("real collection requires a verified adapter or discovery mode")
        return self


class AppConfig(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    service: ServiceSettings
    storage: StorageSettings
    collection: CollectionSettings
    connections: list[ConnectionSettings]
    config_path: Path

    @model_validator(mode="after")
    def validate_connections(self) -> AppConfig:
        connection_ids = [item.connection_id for item in self.connections]
        if len(connection_ids) != len(set(connection_ids)):
            raise ValueError("connection_id values must be unique")
        if not self.service.test_mode and any(item.synthetic_enabled for item in self.connections):
            raise ValueError("synthetic_enabled requires service.test_mode=true")
        for connection in self.connections:
            target = urlsplit(connection.target_page_url)
            response_host = connection.promotion_adapter.response_host.casefold()
            identity_host = connection.promotion_adapter.identity_response_host.casefold()
            if self.service.test_mode:
                if target.hostname not in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                    "yingxiao.pinduoduo.com",
                }:
                    raise ValueError("test target_page_url must be loopback or the fixed PDD host")
            elif target.scheme != "https" or target.hostname != "yingxiao.pinduoduo.com":
                raise ValueError("real target page must use the fixed PDD promotion host")
            if (
                connection.real_collection_enabled
                and connection.promotion_adapter.verified
                and not self.service.test_mode
                and response_host != "yingxiao.pinduoduo.com"
                and not response_host.endswith(".pinduoduo.com")
            ):
                raise ValueError("real response host must be an exact Pinduoduo host")
            if (
                connection.real_collection_enabled
                and connection.promotion_adapter.verified
                and connection.promotion_adapter.identity_response_path
                and not self.service.test_mode
                and identity_host != "yingxiao.pinduoduo.com"
                and not identity_host.endswith(".pinduoduo.com")
            ):
                raise ValueError("real identity response host must be an exact Pinduoduo host")
            core_adapters = (
                connection.promotion_account_adapter,
                connection.promotion_metrics_adapter,
                connection.store_overview_adapter,
                connection.product_catalog_adapter,
                connection.inventory_adapter,
            )
            for adapter in core_adapters:
                if not (adapter.verified or adapter.discovery.enabled):
                    continue
                page_target = urlsplit(adapter.target_page_url)
                allowed_test_host = page_target.hostname in {"127.0.0.1", "localhost", "::1"}
                allowed_real_host = bool(
                    page_target.hostname
                    and (
                        page_target.hostname == "pinduoduo.com"
                        or page_target.hostname.endswith(".pinduoduo.com")
                    )
                )
                if page_target.scheme not in {"http", "https"} or not (
                    allowed_test_host if self.service.test_mode else allowed_real_host
                ):
                    raise ValueError("core target page must use an approved exact host")
                for host in (adapter.response_host, adapter.identity_response_host):
                    if not host:
                        continue
                    if not self.service.test_mode and not (
                        host == "pinduoduo.com" or host.endswith(".pinduoduo.com")
                    ):
                        raise ValueError("core response host must be an exact Pinduoduo host")
        return self

    @property
    def allowed_store_ids(self) -> frozenset[str]:
        return frozenset(item.store_id for item in self.connections)

    def connection(self, connection_id: str) -> ConnectionSettings:
        for item in self.connections:
            if item.connection_id == connection_id:
                return item
        raise KeyError(connection_id)


def load_config(path: Path) -> AppConfig:
    config_path = path.resolve(strict=True)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a table")
    storage = raw.get("storage")
    if not isinstance(storage, dict):
        raise ValueError("configuration requires [storage]")
    for key in ("data_root", "runtime_root"):
        value = storage.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"storage.{key} must be a non-empty string")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = config_path.parent / candidate
        storage[key] = candidate.resolve(strict=False)
    raw["config_path"] = config_path
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc
