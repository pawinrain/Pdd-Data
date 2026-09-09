from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
SNAPSHOT_ID_PATTERN = r"^s_[0-9a-f]{32}$"
INVALIDATION_ID_PATTERN = r"^i_[0-9a-f]{32}$"
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
SAFE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}$"
PLATFORM_ENTITY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"

InternalId = Annotated[str, Field(pattern=ID_PATTERN)]
SnapshotId = Annotated[str, Field(pattern=SNAPSHOT_ID_PATTERN)]
InvalidationId = Annotated[str, Field(pattern=INVALIDATION_ID_PATTERN)]
IdempotencyKey = Annotated[str, Field(pattern=IDEMPOTENCY_PATTERN)]
PlatformEntityId = Annotated[str, Field(pattern=PLATFORM_ENTITY_ID_PATTERN)]
NonNegativeDecimalString = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must include a timezone")
    return value


class DatasetType(StrEnum):
    STORE_OVERVIEW = "store_overview"
    PRODUCT_CATALOG = "product_catalog"
    INVENTORY = "inventory"
    PROMOTION_OVERVIEW = "promotion_overview"
    PRODUCT_METRICS = "product_metrics"
    PRODUCT_BUSINESS_METRICS = "product_business_metrics"
    CAMPAIGN_METRICS = "campaign_metrics"
    PROMOTION_CONFIGURATION = "promotion_configuration"
    ACTIVITY_CATALOG = "activity_catalog"


SUPPORTED_SYNTHETIC_DATASETS = frozenset(
    {
        DatasetType.STORE_OVERVIEW,
        DatasetType.PRODUCT_CATALOG,
        DatasetType.INVENTORY,
        DatasetType.PROMOTION_OVERVIEW,
    }
)


class WindowKind(StrEnum):
    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"
    LAST_90_DAYS = "LAST_90_DAYS"
    POINT_IN_TIME = "POINT_IN_TIME"
    CUSTOM = "CUSTOM"
    UNKNOWN = "UNKNOWN"


class Scope(StrictModel):
    # MCP transports decode JSON dates/enums to strings before invoking tools.  Keep
    # extra-field and business-rule validation strict while accepting their standard
    # JSON representations at this one protocol boundary.
    model_config = ConfigDict(extra="forbid", strict=False)

    kind: WindowKind
    business_date: date
    timezone: str = "Asia/Shanghai"
    object_type: str = Field(default="ACCOUNT_ALL", pattern=ID_PATTERN)
    filters: dict[str, str] = Field(default_factory=dict)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    attribution: str = Field(default="PLATFORM_DEFAULT", pattern=ID_PATTERN)
    version: str = Field(default="1", pattern=SAFE_VERSION_PATTERN)
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def validate_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "scope datetime")

    @field_validator("timezone")
    @classmethod
    def validate_timezone_name(cls, value: str) -> str:
        if not value or len(value) > 64 or ".." in value or "\\" in value:
            raise ValueError("invalid timezone name")
        return value

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("too many filters")
        for key, item in value.items():
            if not key or len(key) > 64 or len(item) > 256:
                raise ValueError("invalid filter")
        return value

    @model_validator(mode="after")
    def validate_custom_window(self) -> Scope:
        if self.kind is WindowKind.CUSTOM:
            if self.start is None or self.end is None:
                raise ValueError("CUSTOM scope requires start and end")
            if self.end <= self.start:
                raise ValueError("scope end must be after start")
        elif self.start is not None or self.end is not None:
            raise ValueError("start/end are accepted only for CUSTOM scope")
        return self


class MetricWindow(StrictModel):
    kind: WindowKind
    timezone: str
    start: datetime
    end: datetime
    window_complete: bool
    source_finalized: bool | None = None

    @field_validator("start", "end")
    @classmethod
    def validate_aware(cls, value: datetime) -> datetime:
        checked = _aware(value, "metric window datetime")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_order(self) -> MetricWindow:
        if self.end <= self.start:
            raise ValueError("metric window end must be after start")
        return self


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class Coverage(StrictModel):
    total_observed: int | None = Field(default=None, ge=0)
    captured: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    pages_read: int = Field(ge=0)
    coverage: CoverageStatus
    truncated: bool
    stop_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_counts(self) -> Coverage:
        if self.captured > self.limit:
            raise ValueError("captured cannot exceed limit")
        if self.total_observed is not None and self.captured > self.total_observed:
            raise ValueError("captured cannot exceed total_observed")
        if self.truncated and self.coverage is not CoverageStatus.TRUNCATED:
            raise ValueError("truncated data must use TRUNCATED coverage")
        if self.coverage is CoverageStatus.TRUNCATED and not self.truncated:
            raise ValueError("TRUNCATED coverage requires truncated=true")
        if (
            self.total_observed is not None
            and self.total_observed > self.captured
            and self.coverage is CoverageStatus.COMPLETE
        ):
            raise ValueError("incomplete known total cannot use COMPLETE coverage")
        if self.coverage is CoverageStatus.PARTIAL and self.truncated:
            raise ValueError("PARTIAL coverage must not be marked truncated")
        return self


class Quality(StrictModel):
    status: Literal["VALID", "INVALID"]
    identity: Literal["MATCHED", "NOT_CHECKED", "MISMATCH"]
    coverage: CoverageStatus
    dom_check: Literal["MATCHED", "NOT_RUN", "MISMATCH"]


class IdentityEvidence(StrictModel):
    expected_platform_store_id: str = Field(min_length=1, max_length=128)
    observed_platform_store_id: str = Field(min_length=1, max_length=128)
    evidence_source: Literal["NETWORK_RESPONSE", "DOM"]
    independent_verification_method: Literal["CONFIG_EXACT_ID", "MERCHANT_PAGE_STATE_SHA256"] = (
        "CONFIG_EXACT_ID"
    )
    independent_verification_reference: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    response_field_path: str | None = Field(default=None, min_length=1, max_length=256)
    dom_selector: str | None = Field(default=None, max_length=512)
    dom_attribute: str | None = Field(default=None, max_length=128)


class MetricValue(StrictModel):
    value: int | str
    unit: Literal["CNY_CENT", "COUNT", "RATIO"]
    observed_at: datetime
    source_updated_at: datetime | None = None
    capture_method: Literal["SYNTHETIC", "NETWORK_RESPONSE", "DOM", "MIXED"]
    precision: Literal["EXACT", "APPROXIMATE"]

    @field_validator("observed_at", "source_updated_at")
    @classmethod
    def validate_observed_at(cls, value: datetime | None) -> datetime | None:
        checked = _aware(value, "observed_at")
        return checked


class StoreOverviewPayload(StrictModel):
    metrics: dict[str, MetricValue | None]


class PromotionOverviewPayload(StrictModel):
    metrics: dict[str, MetricValue | None]


PromotionMetricField = Literal[
    "spend_cents",
    "order_spend_cents",
    "order_spend_roi",
    "order_spend_net_roi",
    "net_order_count",
    "order_count",
    "gmv_cents",
    "net_gmv_cents",
    "impression_count",
    "click_count",
    "settlement_roi",
    "settlement_order_count",
]
PromotionConfigurationField = Literal[
    "max_cost_cents",
    "target_roi",
    "agent_bid",
    "ad_status",
]
MissingReason = Literal["SOURCE_FIELD_MISSING", "SOURCE_VALUE_NULL"]


class PromotionAccountTimeEvidence(StrictModel):
    """Sanitized, non-entity evidence for one account comparison window.

    The source request and DOM text are validated before this model is built.  Only
    normalized dates, cutoff clocks, and fixed provenance labels are persisted; no
    request identity, headers, response body, or metric values are included.
    """

    window_kind: WindowKind
    request_start_date: date = Field(
        description="Normalized request startDate after exact midnight-format validation."
    )
    request_end_date: date = Field(
        description="Normalized request endDate after exact midnight-format validation."
    )
    request_end_day_hour: int = Field(ge=0, le=23)
    response_business_date: date = Field(
        description="Normalized date from the single response dailyReportList row."
    )
    page_today_cutoff_hhmm: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    page_yesterday_cutoff_hhmm: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    page_semantics: Literal["SAME_PERIOD_COMPARISON"] = "SAME_PERIOD_COMPARISON"
    business_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    request_source: Literal["NETWORK_RESPONSE"] = "NETWORK_RESPONSE"
    response_source: Literal["NETWORK_RESPONSE"] = "NETWORK_RESPONSE"
    page_source: Literal["DOM"] = "DOM"
    response_hourly_row_count: int | None = Field(default=None, ge=1, le=24)
    response_first_hour: int | None = Field(default=None, ge=0, le=23)
    response_last_hour: int | None = Field(default=None, ge=0, le=23)

    @model_validator(mode="after")
    def validate_window_evidence(self) -> PromotionAccountTimeEvidence:
        if self.window_kind not in {WindowKind.TODAY, WindowKind.YESTERDAY}:
            raise ValueError("account time evidence supports only TODAY or YESTERDAY")
        if not (self.request_start_date == self.request_end_date == self.response_business_date):
            raise ValueError("account time evidence dates must identify one business day")
        selected_cutoff = (
            self.page_today_cutoff_hhmm
            if self.window_kind is WindowKind.TODAY
            else self.page_yesterday_cutoff_hhmm
        )
        selected_cutoff_hour = int(selected_cutoff[:2])
        if (
            self.window_kind is WindowKind.YESTERDAY
            and selected_cutoff_hour != self.request_end_day_hour
        ) or (
            self.window_kind is WindowKind.TODAY
            and selected_cutoff_hour > self.request_end_day_hour
        ):
            raise ValueError("request endDayHour is inconsistent with the selected page cutoff")
        hourly_values = (
            self.response_hourly_row_count,
            self.response_first_hour,
            self.response_last_hour,
        )
        if any(value is not None for value in hourly_values):
            if any(value is None for value in hourly_values):
                raise ValueError("hourly response evidence must be provided together")
            assert self.response_hourly_row_count is not None
            assert self.response_first_hour is not None
            assert self.response_last_hour is not None
            if (
                self.response_first_hour != 0
                or self.response_last_hour != self.request_end_day_hour
                or self.response_hourly_row_count != self.response_last_hour + 1
            ):
                raise ValueError("hourly response evidence must cover 0 through endDayHour")
        return self


class PromotedProductEffectMetrics(StrictModel):
    """Effect values for one promoted-product row or the response summary."""

    spend_cents: int | None = Field(default=None, ge=0)
    order_spend_cents: int | None = Field(default=None, ge=0)
    order_spend_roi: NonNegativeDecimalString | None = None
    order_spend_net_roi: NonNegativeDecimalString | None = None
    net_order_count: int | None = Field(default=None, ge=0)
    order_count: int | None = Field(default=None, ge=0)
    gmv_cents: int | None = Field(default=None, ge=0)
    net_gmv_cents: int | None = Field(default=None, ge=0)
    impression_count: int | None = Field(default=None, ge=0)
    click_count: int | None = Field(default=None, ge=0)
    settlement_roi: NonNegativeDecimalString | None = None
    settlement_order_count: int | None = Field(default=None, ge=0)
    missing_reasons: dict[PromotionMetricField, MissingReason] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_missing_reasons(self) -> PromotedProductEffectMetrics:
        fields = (
            "spend_cents",
            "order_spend_cents",
            "order_spend_roi",
            "order_spend_net_roi",
            "net_order_count",
            "order_count",
            "gmv_cents",
            "net_gmv_cents",
            "impression_count",
            "click_count",
            "settlement_roi",
            "settlement_order_count",
        )
        missing = {field for field in fields if getattr(self, field) is None}
        reasons = set(self.missing_reasons)
        if missing != reasons:
            raise ValueError("missing_reasons must describe exactly the null effect fields")
        return self


class PromotionAccountMetricPayload(StrictModel):
    """One verified account/day promotion report.

    Account metrics deliberately reuse the fixed twelve-field effect model.  A
    free-form metric mapping would make a changed platform field look like a
    compatible account snapshot.
    """

    entity_granularity: Literal["ACCOUNT"] = "ACCOUNT"
    business_date: date = Field(
        description=(
            "Business date normalized from the exact PDD YYYY-MM-DD 00:00:00 response value."
        )
    )
    metrics: PromotedProductEffectMetrics
    observed_at: datetime
    source_updated_at: datetime | None = Field(
        default=None,
        description="Matched result.reportLastUpdateTime and result.lastUpdateTime value.",
    )
    source_updated_at_missing_reason: MissingReason | None = None

    @field_validator("observed_at", "source_updated_at")
    @classmethod
    def validate_account_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "promotion account datetime")

    @model_validator(mode="after")
    def validate_source_updated_at_reason(self) -> PromotionAccountMetricPayload:
        if self.source_updated_at is None:
            if self.source_updated_at_missing_reason is None:
                raise ValueError("null source_updated_at requires an exact missing reason")
        elif self.source_updated_at_missing_reason is not None:
            raise ValueError("present source_updated_at must not have a missing reason")
        return self


class PromotedProductMetricRecord(StrictModel):
    """One adInfo row; campaign_id is an association, not campaign granularity."""

    entity_granularity: Literal["PROMOTED_PRODUCT"] = "PROMOTED_PRODUCT"
    ad_id: PlatformEntityId
    campaign_id: PlatformEntityId
    platform_product_id: PlatformEntityId
    metrics: PromotedProductEffectMetrics
    observed_at: datetime
    source_updated_at: datetime

    @field_validator("observed_at", "source_updated_at")
    @classmethod
    def validate_promotion_times(cls, value: datetime) -> datetime:
        checked = _aware(value, "promotion metric datetime")
        assert checked is not None
        return checked


class PromotionConfigurationRecord(StrictModel):
    """Current-only settings observed on one promoted-product row."""

    entity_granularity: Literal["PROMOTED_PRODUCT"] = "PROMOTED_PRODUCT"
    ad_id: PlatformEntityId
    campaign_id: PlatformEntityId
    platform_product_id: PlatformEntityId
    max_cost_cents: int | None = Field(default=None, ge=0)
    target_roi: NonNegativeDecimalString | None = None
    agent_bid: NonNegativeDecimalString | None = None
    ad_status: int | None = Field(default=None, ge=0)
    configuration_observed_at: datetime
    missing_reasons: dict[PromotionConfigurationField, MissingReason] = Field(default_factory=dict)

    @field_validator("configuration_observed_at")
    @classmethod
    def validate_configuration_time(cls, value: datetime) -> datetime:
        checked = _aware(value, "promotion configuration datetime")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_missing_reasons(self) -> PromotionConfigurationRecord:
        fields = ("max_cost_cents", "target_roi", "agent_bid", "ad_status")
        missing = {field for field in fields if getattr(self, field) is None}
        reasons = set(self.missing_reasons)
        if missing != reasons:
            raise ValueError("missing_reasons must describe exactly the null configuration fields")
        return self


ProductBusinessSourceField = Literal[
    "payOrdrUsrCnt",
    "payOrdrCnt",
    "payOrdrGoodsQty",
    "payOrdrAmt",
    "goodsUv",
    "goodsPv",
]


class ProductBusinessMetricCandidate(StrictModel):
    """A source string whose numeric format and unit are not yet verified.

    Keeping the exact source string and both verification flags prevents a field
    name from being mistaken for a confirmed count or money unit.  This model is
    parser output only until those semantics are accepted by the snapshot layer.
    """

    source_field: ProductBusinessSourceField
    source_value: str | None = Field(default=None, max_length=128)
    source_value_type: Literal["STRING"] = "STRING"
    numeric_format_verified: Literal[False] = False
    unit_semantics_verified: Literal[False] = False
    missing_reason: MissingReason | None = None

    @field_validator("source_value")
    @classmethod
    def validate_source_value(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("source_value contains a control character")
        return value

    @model_validator(mode="after")
    def validate_missing_reason(self) -> ProductBusinessMetricCandidate:
        if self.source_value is None and self.missing_reason is None:
            raise ValueError("null source_value requires an exact missing reason")
        if self.source_value is not None and self.missing_reason is not None:
            raise ValueError("present source_value must not have a missing reason")
        return self


class ProductBusinessMetricCandidates(StrictModel):
    paying_buyer_count: ProductBusinessMetricCandidate
    paid_order_count: ProductBusinessMetricCandidate
    paid_goods_quantity: ProductBusinessMetricCandidate
    paid_amount: ProductBusinessMetricCandidate
    goods_visitor_count: ProductBusinessMetricCandidate
    goods_page_view_count: ProductBusinessMetricCandidate

    @model_validator(mode="after")
    def validate_source_mapping(self) -> ProductBusinessMetricCandidates:
        expected: dict[str, ProductBusinessSourceField] = {
            "paying_buyer_count": "payOrdrUsrCnt",
            "paid_order_count": "payOrdrCnt",
            "paid_goods_quantity": "payOrdrGoodsQty",
            "paid_amount": "payOrdrAmt",
            "goods_visitor_count": "goodsUv",
            "goods_page_view_count": "goodsPv",
        }
        if any(getattr(self, field).source_field != source for field, source in expected.items()):
            raise ValueError("product business source-field mapping mismatch")
        return self


class ProductBusinessYesterdayCandidateEvidence(StrictModel):
    """Candidate-only date evidence used by the non-persisting parser."""

    window_kind: Literal[WindowKind.YESTERDAY] = WindowKind.YESTERDAY
    business_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    business_date: date
    request_start_date_text: str = Field(min_length=1, max_length=64)
    request_end_date_text: str = Field(min_length=1, max_length=64)
    response_stat_date_text: str = Field(min_length=1, max_length=64)
    window_start: datetime
    window_end: datetime
    window_complete: Literal[True] = True
    source_finalized: None = None
    result_granularity: Literal["DAILY_AGGREGATE"] = "DAILY_AGGREGATE"
    date_semantics_verified: Literal[True]
    pagination_semantics_verified: Literal[False] = False
    source_classification_verified: Literal[False] = False

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_window_times(cls, value: datetime) -> datetime:
        checked = _aware(value, "product business window datetime")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_full_natural_day(self) -> ProductBusinessYesterdayCandidateEvidence:
        zone = ZoneInfo(self.business_timezone)
        expected_start = datetime.combine(self.business_date, time.min, tzinfo=zone)
        expected_end = datetime.combine(
            self.business_date + timedelta(days=1), time.min, tzinfo=zone
        )
        if (
            self.window_start.astimezone(zone) != expected_start
            or self.window_end.astimezone(zone) != expected_end
        ):
            raise ValueError("product business YESTERDAY must be one full natural day")
        return self


class ProductBusinessMetricCandidateRecord(StrictModel):
    """Candidate-only product/day row with unverified metric-string semantics."""

    entity_granularity: Literal["PRODUCT"] = "PRODUCT"
    result_granularity: Literal["DAILY_AGGREGATE"] = "DAILY_AGGREGATE"
    window_kind: Literal[WindowKind.YESTERDAY] = WindowKind.YESTERDAY
    business_date: date
    platform_product_id: str = Field(pattern=r"^[1-9][0-9]{0,127}$")
    metrics: ProductBusinessMetricCandidates
    observed_at: datetime
    source_updated_at: None = None
    source_classification_verified: Literal[False] = False

    @field_validator("observed_at")
    @classmethod
    def validate_product_business_observed_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "product business observed_at")
        assert checked is not None
        return checked


class ProductCatalogRecord(StrictModel):
    product_id: InternalId
    platform_product_id: str | None = Field(default=None, min_length=1, max_length=128)
    sku_id: InternalId | None = None
    name: str | None = Field(default=None, max_length=512)
    price_cents: int | None = Field(default=None, ge=0)
    status: Literal["ON_SALE", "OFF_SALE", "UNKNOWN"] | None = None
    sku_count: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None
    published_at: datetime | None = None
    observed_at: datetime

    @field_validator("created_at", "published_at", "observed_at")
    @classmethod
    def validate_product_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "product datetime")


class InventoryRecord(StrictModel):
    product_id: InternalId
    platform_product_id: str | None = Field(default=None, min_length=1, max_length=128)
    sku_id: InternalId | None = None
    inventory: int | None = Field(default=None, ge=0)
    granularity: Literal["PRODUCT", "SKU"]
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "observed_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_granularity(self) -> InventoryRecord:
        if self.granularity == "SKU" and self.sku_id is None:
            raise ValueError("SKU inventory requires sku_id")
        if self.granularity == "PRODUCT" and self.sku_id is not None:
            raise ValueError("product inventory must not invent sku_id")
        return self


class ValidationReport(StrictModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "checked_at")
        assert checked is not None
        return checked


class SnapshotDraft(StrictModel):
    batch_id: InternalId | None = None
    store_id: InternalId
    dataset_type: DatasetType
    scope: Scope
    scope_key: str = Field(pattern=r"^scope_[0-9a-f]{32}$")
    requested_at: datetime
    capture_started_at: datetime
    capture_finished_at: datetime
    captured_at: datetime
    metric_window: MetricWindow | None = None
    source_updated_at: datetime | None = None
    promotion_account_time_evidence: PromotionAccountTimeEvidence | None = None
    source: Literal["SYNTHETIC", "PDD_BROWSER_CDP"]
    capture_method: Literal["SYNTHETIC", "NETWORK_RESPONSE", "DOM", "MIXED"]
    parser_version: str = Field(pattern=SAFE_VERSION_PATTERN)
    identity_evidence: IdentityEvidence | None = None
    field_sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]] = Field(default_factory=dict)
    payload: dict[str, Any] | list[dict[str, Any]]
    missing_fields: list[str] = Field(default_factory=list)
    quality: Quality
    coverage: Coverage

    @field_validator(
        "requested_at",
        "capture_started_at",
        "capture_finished_at",
        "captured_at",
        "source_updated_at",
    )
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "snapshot datetime")

    @model_validator(mode="after")
    def validate_sequence(self) -> SnapshotDraft:
        if self.capture_finished_at < self.capture_started_at:
            raise ValueError("capture_finished_at precedes capture_started_at")
        if self.captured_at < self.capture_started_at:
            raise ValueError("captured_at precedes capture_started_at")
        if self.source == "SYNTHETIC" and self.capture_method != "SYNTHETIC":
            raise ValueError("synthetic data must use synthetic capture method")
        return self


class FileDescriptor(StrictModel):
    name: Literal["data.json", "records.jsonl", "validation.json"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    records: int = Field(ge=0)


class SnapshotManifest(StrictModel):
    snapshot_id: SnapshotId
    request_id: InternalId
    batch_id: InternalId | None = None
    idempotency_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["1.0.0", "1.1.0"] = "1.0.0"
    layout_version: str
    platform: Literal["pdd"] = "pdd"
    store_id: InternalId
    dataset_type: DatasetType
    scope: Scope
    scope_key: str = Field(pattern=r"^scope_[0-9a-f]{32}$")
    requested_at: datetime
    capture_started_at: datetime
    capture_finished_at: datetime
    captured_at: datetime
    metric_window: MetricWindow | None = None
    source_updated_at: datetime | None = None
    promotion_account_time_evidence: PromotionAccountTimeEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    committed_at: datetime
    source: Literal["SYNTHETIC", "PDD_BROWSER_CDP"]
    capture_method: Literal["SYNTHETIC", "NETWORK_RESPONSE", "DOM", "MIXED"]
    parser_version: str
    identity_evidence: IdentityEvidence | None = None
    field_sources: dict[str, Literal["NETWORK_RESPONSE", "DOM"]] = Field(default_factory=dict)
    missing_fields: list[str]
    quality: Quality
    coverage: Coverage
    payload_file: Literal["data.json", "records.jsonl"]
    files: list[FileDescriptor]
    record_count: int = Field(ge=0)

    @field_validator(
        "requested_at",
        "capture_started_at",
        "capture_finished_at",
        "captured_at",
        "source_updated_at",
        "committed_at",
    )
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "manifest datetime")

    @model_validator(mode="after")
    def validate_schema_version_features(self) -> SnapshotManifest:
        if self.scope.version == "d4-account-v3":
            if self.schema_version != "1.1.0":
                raise ValueError("d4-account-v3 requires manifest schema 1.1.0")
            evidence = self.promotion_account_time_evidence
            window = self.metric_window
            if evidence is None or window is None:
                raise ValueError("d4-account-v3 manifest requires time evidence and metric window")
            if (
                self.dataset_type is not DatasetType.PROMOTION_OVERVIEW
                or self.scope.object_type != "ACCOUNT_ALL"
                or self.scope.kind not in {WindowKind.TODAY, WindowKind.YESTERDAY}
                or self.source != "PDD_BROWSER_CDP"
                or window.window_complete
                or window.source_finalized is not False
            ):
                raise ValueError("d4-account-v3 manifest account contract mismatch")
            if (
                self.parser_version != self.scope.version
                or window.kind is not self.scope.kind
                or evidence.window_kind is not self.scope.kind
                or window.timezone != self.scope.timezone
                or evidence.business_timezone != self.scope.timezone
            ):
                raise ValueError("d4-account-v3 manifest scope/parser/window mismatch")
            row_count = evidence.response_hourly_row_count
            first_hour = evidence.response_first_hour
            last_hour = evidence.response_last_hour
            if row_count is None or first_hour is None or last_hour is None:
                raise ValueError("d4-account-v3 manifest requires hourly response evidence")
            if (
                first_hour != 0
                or last_hour != evidence.request_end_day_hour
                or row_count != last_hour + 1
            ):
                raise ValueError("d4-account-v3 manifest hourly response evidence mismatch")
            if (
                evidence.request_start_date != self.scope.business_date
                or evidence.request_end_date != self.scope.business_date
                or evidence.response_business_date != self.scope.business_date
            ):
                raise ValueError("d4-account-v3 manifest business date evidence mismatch")
            zone = ZoneInfo(self.scope.timezone)
            start_local = window.start.astimezone(zone)
            if start_local.date() != self.scope.business_date or start_local.time() != time.min:
                raise ValueError("d4-account-v3 manifest window start mismatch")
            if self.scope.kind is WindowKind.YESTERDAY and window.end != window.start + timedelta(
                hours=last_hour + 1
            ):
                raise ValueError("d4-account-v3 YESTERDAY half-open window mismatch")
            if self.scope.kind is WindowKind.YESTERDAY and (
                evidence.page_yesterday_cutoff_hhmm != f"{last_hour:02d}:59"
            ):
                raise ValueError("d4-account-v3 YESTERDAY cutoff mismatch")
            if self.scope.kind is WindowKind.TODAY:
                if self.source_updated_at is None or window.end != self.source_updated_at:
                    raise ValueError("d4-account-v3 TODAY source update mismatch")
                expected_cutoff = self.source_updated_at.astimezone(zone).strftime("%H:%M")
                if evidence.page_today_cutoff_hhmm != expected_cutoff:
                    raise ValueError("d4-account-v3 TODAY cutoff mismatch")
        elif self.schema_version == "1.0.0" and self.promotion_account_time_evidence is not None:
            raise ValueError("manifest schema 1.0.0 cannot contain account time evidence")
        return self


class SnapshotInvalidationRecord(StrictModel):
    """Append-only semantic invalidation stored outside an immutable snapshot."""

    invalidation_id: InvalidationId
    snapshot_id: SnapshotId
    store_id: InternalId
    dataset_type: DatasetType
    invalidated_at: datetime
    reason_code: Literal["METRIC_WINDOW_END_MISLABELED"]
    prior_scope_version: str = Field(pattern=SAFE_VERSION_PATTERN)
    prior_parser_version: str = Field(pattern=SAFE_VERSION_PATTERN)
    replacement_scope_version: str = Field(pattern=SAFE_VERSION_PATTERN)

    @field_validator("invalidated_at")
    @classmethod
    def validate_invalidated_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "invalidated_at")
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_version_transition(self) -> SnapshotInvalidationRecord:
        if self.replacement_scope_version == self.prior_scope_version:
            raise ValueError("replacement scope version must differ from the invalidated version")
        return self


class CommitRecord(StrictModel):
    snapshot_id: SnapshotId
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def validate_committed_at(cls, value: datetime) -> datetime:
        checked = _aware(value, "committed_at")
        assert checked is not None
        return checked


DatasetCapabilityStatus = Literal[
    "SYNTHETIC_TEST_ONLY",
    "REAL_PROMOTION_TODAY",
    "REAL_PROMOTION_WINDOWS",
    "REAL_PROMOTED_PRODUCT_METRICS",
    "REAL_PROMOTION_CONFIGURATION_CURRENT",
    "REAL_STORE_TODAY",
    "REAL_PRODUCT_CATALOG",
    "REAL_INVENTORY",
    "CONFIGURED_NOT_VERIFIED",
    "UNAVAILABLE",
]


class DatasetCapabilityDetail(StrictModel):
    status: DatasetCapabilityStatus
    supported_window_kinds: list[WindowKind] = Field(default_factory=list)
    entity_granularity: Literal[
        "STORE", "ACCOUNT", "CAMPAIGN", "PROMOTED_PRODUCT", "PRODUCT", "SKU", "UNKNOWN"
    ]
    current_only: bool = False
    verified: bool = False
    limitation: str | None = Field(default=None, max_length=512)


class CapabilitiesResult(StrictModel):
    service: Literal["pdd-data-mcp"] = "pdd-data-mcp"
    version: str
    transport: Literal["stdio"] = "stdio"
    storage: Literal["local_files"] = "local_files"
    synthetic_test_mode: bool
    real_collection: Literal["DISABLED", "CONFIGURED_NOT_VERIFIED", "AVAILABLE"]
    datasets: dict[str, DatasetCapabilityStatus]
    dataset_details: dict[str, DatasetCapabilityDetail] = Field(default_factory=dict)


class ConnectionStatusResult(StrictModel):
    connection_id: InternalId
    store_id: InternalId
    status: Literal["NOT_CHECKED"] = "NOT_CHECKED"
    real_collection_enabled: bool
    synthetic_test_enabled: bool
    message: str


class CollectResult(StrictModel):
    status: Literal[
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "BUSY",
        "INTERRUPTED",
        "REAL_COLLECTION_DISABLED",
        "DATASET_UNVERIFIED",
        "ADAPTER_UNVERIFIED",
        "CDP_UNAVAILABLE",
        "TARGET_PAGE_NOT_FOUND",
        "AUTH_REQUIRED",
        "IDENTITY_UNVERIFIED",
        "IDENTITY_MISMATCH",
        "PLATFORM_ERROR",
        "TIME_SCOPE_UNVERIFIED",
        "UNIT_UNVERIFIED",
        "DATA_MISMATCH",
        "CAPTURE_TIMEOUT",
    ]
    committed: bool
    snapshot_id: SnapshotId | None = None
    batch_id: InternalId | None = None
    dataset_type: DatasetType
    captured: int = 0
    total_observed: int | None = None
    coverage: CoverageStatus = CoverageStatus.UNKNOWN
    truncated: bool = False
    idempotent_replay: bool = False
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class SnapshotSummary(StrictModel):
    snapshot_id: SnapshotId
    store_id: InternalId
    dataset_type: DatasetType
    scope_key: str
    captured_at: datetime
    committed_at: datetime
    source: Literal["SYNTHETIC", "PDD_BROWSER_CDP"]
    quality_status: Literal["VALID", "INVALID"]
    effective_status: Literal[
        "ACTIVE", "SEMANTICALLY_INVALIDATED", "INVALIDATION_STATE_UNKNOWN"
    ] = Field(default="ACTIVE", exclude_if=lambda value: value == "ACTIVE")
    invalidation: SnapshotInvalidationRecord | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    coverage: CoverageStatus
    record_count: int

    @model_validator(mode="after")
    def validate_effective_status(self) -> SnapshotSummary:
        if (self.effective_status == "SEMANTICALLY_INVALIDATED") != (self.invalidation is not None):
            raise ValueError("semantic invalidation status and record must be paired")
        if self.invalidation is not None and (
            self.invalidation.snapshot_id != self.snapshot_id
            or self.invalidation.store_id != self.store_id
            or self.invalidation.dataset_type is not self.dataset_type
        ):
            raise ValueError("semantic invalidation identity does not match snapshot summary")
        return self


class ListSnapshotsResult(StrictModel):
    items: list[SnapshotSummary]
    next_cursor: str | None = None


class ReadSnapshotResult(StrictModel):
    snapshot_id: SnapshotId
    manifest: dict[str, Any]
    effective_status: Literal[
        "ACTIVE", "SEMANTICALLY_INVALIDATED", "INVALIDATION_STATE_UNKNOWN"
    ] = Field(default="ACTIVE", exclude_if=lambda value: value == "ACTIVE")
    invalidation: SnapshotInvalidationRecord | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    data: dict[str, Any] | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def validate_effective_status(self) -> ReadSnapshotResult:
        if (self.effective_status == "SEMANTICALLY_INVALIDATED") != (self.invalidation is not None):
            raise ValueError("semantic invalidation status and record must be paired")
        if self.manifest.get("snapshot_id") != self.snapshot_id:
            raise ValueError("read result snapshot identity does not match manifest")
        if self.invalidation is not None and (
            self.invalidation.snapshot_id != self.snapshot_id
            or self.invalidation.store_id != self.manifest.get("store_id")
            or self.invalidation.dataset_type.value != self.manifest.get("dataset_type")
        ):
            raise ValueError("semantic invalidation identity does not match read result")
        return self


class LatestSnapshotResult(StrictModel):
    status: Literal["FOUND", "NOT_FOUND", "PARTIAL_ONLY"]
    snapshot: SnapshotSummary | None = None
    message: str | None = None
