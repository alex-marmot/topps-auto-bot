from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

BEIJING_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc
USD = "USD"

DEFAULT_WINDOW_SECONDS = 60
MIN_WINDOW_SECONDS = 1
MAX_WINDOW_SECONDS = 600

_USD_TEXT = re.compile(r"^\d+(?:\.\d{1,2})?$")
_BEIJING_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")


class DomainError(ValueError):
    """Raised when a domain value violates a purchase constraint."""


def parse_usd(value: object, *, allow_zero: bool = False) -> Decimal:
    """Parse a USD amount without ever going through binary floating point."""
    if isinstance(value, bool):
        raise DomainError("金额必须是十进制数字，不能是布尔值")
    if isinstance(value, float):
        raise DomainError("金额不能使用浮点数表示")
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _USD_TEXT.fullmatch(text):
            raise DomainError("金额必须是最多两位小数的十进制 USD 数值")
        decimal_value = Decimal(text)
    else:
        raise DomainError("金额必须是十进制数字或字符串")
    if not decimal_value.is_finite():
        raise DomainError("金额必须是有限数值")
    if decimal_value < 0:
        raise DomainError("金额不能为负数")
    if decimal_value == 0 and not allow_zero:
        raise DomainError("金额必须大于零")
    if decimal_value.as_tuple().exponent < -2:
        raise DomainError("金额最多保留两位小数")
    return decimal_value.quantize(Decimal("0.01"))


def parse_quantity(value: object) -> int:
    if isinstance(value, bool):
        raise DomainError("数量必须是正整数，不能是布尔值")
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise DomainError("数量必须是正整数")
        quantity = int(text)
    else:
        raise DomainError("数量必须是正整数")
    if quantity <= 0:
        raise DomainError("数量必须大于零")
    return quantity


def format_usd(value: Decimal) -> str:
    return f"USD {value.quantize(Decimal('0.01')):.2f}"


def normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def parse_beijing_datetime(value: str) -> datetime:
    text = value.strip()
    if not _BEIJING_TEXT.fullmatch(text):
        raise DomainError("时间格式必须为 YYYY-MM-DD HH:MM:SS（北京时间）")
    parsed = datetime.strptime(text.replace("T", " "), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=BEIJING_TZ)


def format_beijing_datetime(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def utc_now_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DomainError("UTC 时间必须包含时区")
    return parsed.astimezone(UTC)


def target_url_key(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/").casefold()
    return f"{host}{path}"


class ScheduleMode(StrEnum):
    IMMEDIATE = "immediate"
    SCHEDULED = "scheduled"


class PreparedLevel(StrEnum):
    NONE = "NONE"
    CART_READY = "CART_READY"
    CHECKOUT_READY = "CHECKOUT_READY"


class TaskPhase(StrEnum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    AUTO_LOGIN = "AUTO_LOGIN"
    AWAITING_MANUAL_LOGIN = "AWAITING_MANUAL_LOGIN"
    AWAITING_LOGIN = "AWAITING_LOGIN"
    RUN_READY = "RUN_READY"
    OPENING_PRODUCT = "OPENING_PRODUCT"
    WAITING_AVAILABILITY = "WAITING_AVAILABILITY"
    ADDING_TO_CART = "ADDING_TO_CART"
    VERIFYING_CART = "VERIFYING_CART"
    CART_READY = "CART_READY"
    CHECKOUT_READY = "CHECKOUT_READY"
    WAITING_WINDOW = "WAITING_WINDOW"
    CHECKING_OUT = "CHECKING_OUT"
    REVALIDATING = "REVALIDATING"
    SUBMITTING = "SUBMITTING"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    EXPIRED = "EXPIRED"
    MISSED = "MISSED"
    UNSUPPORTED = "UNSUPPORTED"
    BLOCKED = "BLOCKED"


class LoginStatus(StrEnum):
    LOGGED_IN = "LOGGED_IN"
    NOT_LOGGED_IN = "NOT_LOGGED_IN"
    CHALLENGE = "CHALLENGE"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    UNRECOGNIZED = "UNRECOGNIZED"
    SESSION_LOST = "SESSION_LOST"


class ReadStatus(StrEnum):
    OK = "OK"
    EMPTY = "EMPTY"
    NOT_LOGGED_IN = "NOT_LOGGED_IN"
    SESSION_LOST = "SESSION_LOST"
    BLOCKED = "BLOCKED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    UNRECOGNIZED = "UNRECOGNIZED"
    ERROR = "ERROR"


class PageKind(StrEnum):
    CART = "CART"
    CHECKOUT = "CHECKOUT"
    PRODUCT = "PRODUCT"
    LOGIN = "LOGIN"
    BLOCKED = "BLOCKED"
    OTHER = "OTHER"


class OrderStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


class SubmissionState(StrEnum):
    SUBMITTING = "SUBMITTING"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    UNKNOWN = "UNKNOWN"


class AddToCartState(StrEnum):
    IN_FLIGHT = "IN_FLIGHT"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    NOT_SENT = "NOT_SENT"


@dataclass(frozen=True)
class ProductPreset:
    preset_id: str
    title: str
    variant: str
    url: str
    quantity: int
    unit_price_limit_usd: Decimal

    @property
    def identity_key(self) -> tuple[str, str]:
        return (normalize_text(self.title), normalize_text(self.variant))

    @property
    def url_key(self) -> str:
        return target_url_key(self.url)


@dataclass(frozen=True)
class LoginCredentials:
    email: str
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class LoginRead:
    status: LoginStatus
    account_identity: str | None = None
    page_url: str | None = None
    error: str | None = None
    challenge: bool = False


@dataclass(frozen=True)
class Customer:
    full_name: str
    email: str
    phone: str
    address_line1: str
    address_line2: str
    city: str
    region: str
    postal_code: str
    country: str


@dataclass(frozen=True)
class Card:
    number: str
    expiry_month: int
    expiry_year: int
    cvv: str
    holder_name: str


@dataclass(frozen=True)
class DriverSettings:
    mode: str = "automatic"
    path: str | None = None
    version: str | None = None
    platform: str | None = None
    no_sandbox: bool = False


@dataclass(frozen=True)
class ScheduleSettings:
    mode: ScheduleMode = ScheduleMode.IMMEDIATE
    scheduled_start_utc: datetime | None = None
    window_seconds: int = DEFAULT_WINDOW_SECONDS

    def planned_start_beijing(self) -> datetime | None:
        if self.scheduled_start_utc is None:
            return None
        return self.scheduled_start_utc.astimezone(BEIJING_TZ)

    def planned_deadline_utc(self) -> datetime | None:
        if self.scheduled_start_utc is None:
            return None
        return self.scheduled_start_utc + timedelta(seconds=self.window_seconds)


@dataclass(frozen=True)
class AppConfig:
    schema_version: int
    presets: tuple[ProductPreset, ...]
    selected_preset_id: str | None
    order_limit_usd: Decimal
    login: LoginCredentials
    customer: Customer
    card: Card
    driver: DriverSettings
    schedule: ScheduleSettings

    def selected_preset(self) -> ProductPreset | None:
        if self.selected_preset_id is None:
            return None
        matches = [p for p in self.presets if p.preset_id == self.selected_preset_id]
        if len(matches) != 1:
            return None
        return matches[0]

    def target_snapshot(self) -> "TargetSnapshot | None":
        preset = self.selected_preset()
        if preset is None:
            return None
        return TargetSnapshot(preset=preset, order_limit_usd=self.order_limit_usd)


@dataclass(frozen=True)
class TargetSnapshot:
    preset: ProductPreset
    order_limit_usd: Decimal


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    target: TargetSnapshot
    login: LoginCredentials
    customer: Customer
    card: Card
    driver: DriverSettings
    schedule: ScheduleSettings
    created_at_utc: datetime


@dataclass(frozen=True)
class ProductCandidate:
    title: str
    url: str
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductRead:
    status: ReadStatus
    title: str | None = None
    variants: tuple[str, ...] = ()
    url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProductSearchRead:
    status: ReadStatus
    candidates: tuple[ProductCandidate, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ProductPageState:
    status: ReadStatus
    title: str | None = None
    url: str | None = None
    variants: tuple[str, ...] = ()
    selected_variant: str | None = None
    quantity: int | None = None
    unit_price_usd: Decimal | None = None
    currency: str | None = None
    add_to_cart_enabled: bool = False
    single_variant: bool = False
    error: str | None = None


@dataclass(frozen=True)
class OrderLine:
    title: str
    variant: str
    quantity: int
    unit_price_usd: Decimal | None = None
    product_key: str | None = None

    @property
    def identity_key(self) -> tuple[str, str]:
        return (normalize_text(self.title), normalize_text(self.variant))


@dataclass(frozen=True)
class ShippingOption:
    service_id: str
    name: str
    amount_usd: Decimal | None
    selected: bool


@dataclass(frozen=True)
class ShippingSnapshot:
    service_id: str
    name: str
    amount_usd: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "amount_usd": f"{self.amount_usd:.2f}",
        }


@dataclass(frozen=True)
class FormState:
    contact_complete: bool
    card_complete: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CartRead:
    status: ReadStatus
    lines: tuple[OrderLine, ...] = ()
    currency: str | None = None
    logged_in: bool | None = None
    page_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CheckoutRead:
    status: ReadStatus
    lines: tuple[OrderLine, ...] = ()
    currency: str | None = None
    subtotal_usd: Decimal | None = None
    tax_usd: Decimal | None = None
    shipping_total_usd: Decimal | None = None
    total_usd: Decimal | None = None
    shipping_options: tuple[ShippingOption, ...] = ()
    form: FormState | None = None
    final_submit_enabled: bool = False
    payment_origin: str | None = None
    page_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Readiness:
    level: PreparedLevel
    reasons: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.level is not PreparedLevel.NONE


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    error: str | None = None
    uncertain: bool = False
    attempted: bool = False
    confirmed_no_side_effect: bool = False


@dataclass(frozen=True)
class SubmitActionResult:
    attempted: bool
    sent: bool | None
    error: str | None = None


@dataclass(frozen=True)
class OrderResult:
    status: OrderStatus
    order_id: str | None = None
    payment_state: str | None = None
    evidence: str | None = None
    matches_target: bool = False
    lines: tuple[OrderLine, ...] = ()


@dataclass(frozen=True)
class ActiveWindow:
    mode: ScheduleMode
    started_at_utc: datetime
    effective_start_utc: datetime
    deadline_utc: datetime | None = None

    def remaining_seconds(self, now: datetime) -> float:
        if self.deadline_utc is None:
            return 0.0
        return max(0.0, (self.deadline_utc - now).total_seconds())

    def expired(self, now: datetime) -> bool:
        return self.deadline_utc is not None and now >= self.deadline_utc


@dataclass(frozen=True)
class TaskEvent:
    at_utc: datetime
    phase: TaskPhase
    message: str
    details: dict[str, Any] = field(default_factory=dict)
