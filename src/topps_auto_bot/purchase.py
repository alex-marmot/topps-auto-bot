from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CartRead,
    CheckoutRead,
    DomainError,
    FormState,
    OrderLine,
    OrderResult,
    OrderStatus,
    PreparedLevel,
    ProductPreset,
    ReadStatus,
    Readiness,
    ShippingSnapshot,
    SubmissionState,
    USD,
    format_usd,
    normalize_text,
    parse_usd,
    utc_now_iso,
)
from .settings import currency_is_usd

SUBMISSION_SCHEMA_VERSION = 1
_ACCEPTED_PAYMENT_STATES = (
    "paid",
    "authorized",
    "accepted",
    "captured",
    "succeeded",
    "success",
    "approved",
)
_DECLINED_PAYMENT_STATES = ("declined", "rejected", "failed", "denied")

_SUBMISSION_KEYS = {
    "schema_version",
    "task_id",
    "state",
    "created_at_utc",
    "updated_at_utc",
    "prepared_level",
    "planned_start_utc",
    "deadline_utc",
    "target",
    "order_limit_usd",
    "shipping",
    "total_usd",
    "outcome",
    "order_id",
    "payment_state",
    "note",
}
_SUBMISSION_TARGET_KEYS = {"title", "variant", "quantity", "unit_price_limit_usd"}
_SUBMISSION_SHIPPING_KEYS = {"service_id", "name", "amount_usd"}


class SubmissionBlocked(RuntimeError):
    pass


class SubmissionStoreError(RuntimeError):
    pass


class SubmissionLoadStatus(StrEnum):
    MISSING = "MISSING"
    OK = "OK"
    CORRUPT = "CORRUPT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    IO_ERROR = "IO_ERROR"


@dataclass(frozen=True)
class SubmissionLoad:
    status: SubmissionLoadStatus
    record: "SubmissionRecord | None" = None
    error: str | None = None

    @property
    def blocking(self) -> bool:
        if self.status in (
            SubmissionLoadStatus.CORRUPT,
            SubmissionLoadStatus.UNSUPPORTED_VERSION,
            SubmissionLoadStatus.IO_ERROR,
        ):
            return True
        if self.record is None:
            return False
        return self.record.state in (SubmissionState.SUBMITTING, SubmissionState.UNKNOWN)


@dataclass(frozen=True)
class SubmissionRecord:
    schema_version: int
    task_id: str
    state: SubmissionState
    created_at_utc: str
    updated_at_utc: str
    prepared_level: PreparedLevel
    planned_start_utc: str | None
    deadline_utc: str
    target_title: str
    target_variant: str
    target_quantity: int
    unit_price_limit_usd: str
    order_limit_usd: str
    shipping_service_id: str
    shipping_service_name: str
    shipping_amount_usd: str
    total_usd: str
    outcome: str | None = None
    order_id: str | None = None
    payment_state: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "state": str(self.state),
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "prepared_level": str(self.prepared_level),
            "planned_start_utc": self.planned_start_utc,
            "deadline_utc": self.deadline_utc,
            "target": {
                "title": self.target_title,
                "variant": self.target_variant,
                "quantity": self.target_quantity,
                "unit_price_limit_usd": self.unit_price_limit_usd,
            },
            "order_limit_usd": self.order_limit_usd,
            "shipping": {
                "service_id": self.shipping_service_id,
                "name": self.shipping_service_name,
                "amount_usd": self.shipping_amount_usd,
            },
            "total_usd": self.total_usd,
            "outcome": self.outcome,
            "order_id": self.order_id,
            "payment_state": self.payment_state,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SubmissionRecord":
        unknown = set(payload) - _SUBMISSION_KEYS
        if unknown:
            raise DomainError("提交记录包含不允许的字段：" + "、".join(sorted(unknown)))
        target = payload.get("target")
        shipping = payload.get("shipping")
        if not isinstance(target, Mapping) or set(target) - _SUBMISSION_TARGET_KEYS:
            raise DomainError("提交记录的目标字段无效")
        if not isinstance(shipping, Mapping) or set(shipping) - _SUBMISSION_SHIPPING_KEYS:
            raise DomainError("提交记录的配送字段无效")
        try:
            version = int(payload["schema_version"])
            state = SubmissionState(str(payload["state"]))
            prepared = PreparedLevel(str(payload["prepared_level"]))
            quantity = int(target["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("提交记录的必要字段无效") from exc
        if version != SUBMISSION_SCHEMA_VERSION:
            raise DomainError("提交记录版本不受支持")
        if quantity <= 0:
            raise DomainError("提交记录数量无效")
        for key in ("task_id", "created_at_utc", "updated_at_utc", "deadline_utc"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise DomainError(f"提交记录字段 {key} 无效")
        planned = payload.get("planned_start_utc")
        if planned is not None and not isinstance(planned, str):
            raise DomainError("提交记录计划时间无效")
        for key in ("order_limit_usd", "total_usd"):
            parse_usd(payload.get(key))
        parse_usd(target.get("unit_price_limit_usd"))
        parse_usd(shipping.get("amount_usd"), allow_zero=True)
        for key in ("title", "variant", "service_id", "name"):
            container = target if key in ("title", "variant") else shipping
            if not isinstance(container.get(key), str) or not container[key].strip():
                raise DomainError(f"提交记录字段 {key} 无效")
        for key in ("outcome", "order_id", "payment_state", "note"):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise DomainError(f"提交记录字段 {key} 必须是文本或 null")
        return cls(
            schema_version=version,
            task_id=payload["task_id"],
            state=state,
            created_at_utc=payload["created_at_utc"],
            updated_at_utc=payload["updated_at_utc"],
            prepared_level=prepared,
            planned_start_utc=planned,
            deadline_utc=payload["deadline_utc"],
            target_title=target["title"],
            target_variant=target["variant"],
            target_quantity=quantity,
            unit_price_limit_usd=str(target["unit_price_limit_usd"]),
            order_limit_usd=str(payload["order_limit_usd"]),
            shipping_service_id=shipping["service_id"],
            shipping_service_name=shipping["name"],
            shipping_amount_usd=str(shipping["amount_usd"]),
            total_usd=str(payload["total_usd"]),
            outcome=payload.get("outcome"),
            order_id=payload.get("order_id"),
            payment_state=payload.get("payment_state"),
            note=payload.get("note"),
        )


class SubmissionStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SubmissionLoad:
        if not self._path.exists():
            return SubmissionLoad(SubmissionLoadStatus.MISSING)
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            return SubmissionLoad(SubmissionLoadStatus.IO_ERROR, error=str(exc))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return SubmissionLoad(SubmissionLoadStatus.CORRUPT, error=f"提交记录不是有效 JSON：{exc.msg}")
        if not isinstance(payload, Mapping):
            return SubmissionLoad(SubmissionLoadStatus.CORRUPT, error="提交记录根节点必须是对象")
        version = payload.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return SubmissionLoad(SubmissionLoadStatus.CORRUPT, error="提交记录缺少有效版本")
        if version > SUBMISSION_SCHEMA_VERSION:
            return SubmissionLoad(
                SubmissionLoadStatus.UNSUPPORTED_VERSION,
                error=f"提交记录版本 {version} 高于当前支持版本",
            )
        try:
            return SubmissionLoad(SubmissionLoadStatus.OK, SubmissionRecord.from_dict(payload))
        except (DomainError, ValueError, TypeError) as exc:
            return SubmissionLoad(SubmissionLoadStatus.CORRUPT, error=str(exc))

    def _write(self, record: SubmissionRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        os.close(descriptor)
        temp_path = Path(raw_path)
        try:
            with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self._path)
            try:
                self._path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SubmissionStoreError(f"提交记录保存失败：{exc}") from exc

    def begin(self, record: SubmissionRecord) -> None:
        existing = self.load()
        if existing.blocking:
            raise SubmissionBlocked("已有未决购买提交，必须先核对结果")
        if existing.status is SubmissionLoadStatus.OK and existing.record is not None:
            if existing.record.state in (SubmissionState.SUBMITTING, SubmissionState.UNKNOWN):
                raise SubmissionBlocked("已有未决购买提交，必须先核对结果")
        self._write(record)

    def update(self, record: SubmissionRecord, *, now: datetime) -> SubmissionRecord:
        if record.task_id.strip() == "":
            raise SubmissionStoreError("提交记录任务标识不能为空")
        updated = replace(record, updated_at_utc=utc_now_iso(now))
        self._write(updated)
        return updated

    def resolve(
        self,
        *,
        state: SubmissionState,
        note: str,
        now: datetime,
    ) -> SubmissionRecord:
        if state not in (
            SubmissionState.SUCCEEDED,
            SubmissionState.DECLINED,
            SubmissionState.NOT_SUBMITTED,
        ):
            raise SubmissionStoreError("人工核对结论必须是成功、拒绝或确认未形成订单")
        existing = self.load()
        if existing.status is not SubmissionLoadStatus.OK or existing.record is None:
            raise SubmissionStoreError("没有可解除的有效提交记录")
        if not existing.blocking:
            raise SubmissionStoreError("当前没有未决购买提交，无需解除阻断")
        updated = replace(
            existing.record,
            state=state,
            outcome=state.value,
            note=note,
            updated_at_utc=utc_now_iso(now),
        )
        self._write(updated)
        return updated


def _read_failure(status: ReadStatus) -> str:
    return {
        ReadStatus.EMPTY: "购物车为空",
        ReadStatus.NOT_LOGGED_IN: "尚未在专用浏览器会话中登录",
        ReadStatus.SESSION_LOST: "浏览器会话或登录状态已失效",
        ReadStatus.BLOCKED: "页面被官网访问限制拦截",
        ReadStatus.NOT_AVAILABLE: "官网当前未提供该页面",
        ReadStatus.UNRECOGNIZED: "无法识别当前页面或已准备内容",
        ReadStatus.ERROR: "读取已准备内容失败",
    }.get(status, "无法确认已准备内容")


@dataclass(frozen=True)
class ContentCheck:
    ok: bool
    reason: str | None = None
    line: OrderLine | None = None


def match_single_target(
    lines: tuple[OrderLine, ...],
    preset: ProductPreset,
    *,
    currency: str | None,
) -> ContentCheck:
    if not currency_is_usd(currency):
        return ContentCheck(False, "购物车或订单币种不是 USD")
    if not lines:
        return ContentCheck(False, "购物车或订单为空")
    if len(lines) != 1:
        return ContentCheck(False, "购物车或订单包含额外或重复条目")
    line = lines[0]
    if line.identity_key != preset.identity_key:
        return ContentCheck(False, "商品身份或规格与本次唯一目标不一致")
    if line.quantity != preset.quantity:
        return ContentCheck(False, f"商品数量应为 {preset.quantity}，当前为 {line.quantity}")
    if line.unit_price_usd is None:
        return ContentCheck(False, "当前 USD 单价无法确认")
    if line.unit_price_usd > preset.unit_price_limit_usd:
        return ContentCheck(
            False,
            f"当前单价 {format_usd(line.unit_price_usd)} 超过上限 {format_usd(preset.unit_price_limit_usd)}",
        )
    return ContentCheck(True, line=line)


def evaluate_cart(cart: CartRead, preset: ProductPreset) -> Readiness:
    if cart.status is not ReadStatus.OK:
        return Readiness(PreparedLevel.NONE, (_read_failure(cart.status),))
    if cart.logged_in is False:
        return Readiness(PreparedLevel.NONE, ("尚未在专用浏览器会话中登录",))
    if cart.logged_in is None:
        return Readiness(PreparedLevel.NONE, ("无法确认专用浏览器会话的登录状态",))
    content = match_single_target(cart.lines, preset, currency=cart.currency)
    if not content.ok:
        return Readiness(PreparedLevel.NONE, (content.reason or "购物车内容不符合唯一目标",))
    line = content.line
    assert line is not None
    return Readiness(
        PreparedLevel.CART_READY,
        (
            "购物车已准备；结算页仍需核对税费、运费、总额和付款表单",
            "购物车已准备不等于已锁定库存或已可付款",
        ),
        {
            "title": line.title,
            "variant": line.variant,
            "quantity": line.quantity,
            "unit_price_usd": format_usd(line.unit_price_usd or Decimal("0")),
        },
    )


def selected_shipping_options(checkout: CheckoutRead):
    return tuple(option for option in checkout.shipping_options if option.selected)


def freeze_shipping(checkout: CheckoutRead) -> tuple[ShippingSnapshot | None, str | None]:
    selected = selected_shipping_options(checkout)
    if len(selected) != 1:
        return None, "无法确认网站唯一明确选中的默认配送服务"
    option = selected[0]
    if not option.service_id.strip() or not option.name.strip():
        return None, "默认配送服务身份不明确"
    if option.amount_usd is None:
        return None, "默认配送运费无法确认"
    if checkout.shipping_total_usd is None:
        return None, "订单运费尚未确定"
    if checkout.shipping_total_usd != option.amount_usd:
        return None, "默认配送服务运费与订单计入运费不一致"
    return (
        ShippingSnapshot(
            service_id=option.service_id,
            name=option.name,
            amount_usd=option.amount_usd,
        ),
        None,
    )


def verify_shipping_snapshot(
    checkout: CheckoutRead,
    snapshot: ShippingSnapshot,
) -> str | None:
    current, reason = freeze_shipping(checkout)
    if current is None:
        return reason
    if current.service_id != snapshot.service_id or normalize_text(current.name) != normalize_text(snapshot.name):
        return (
            "配送服务发生变化：原锁定 "
            f"{snapshot.name}（{snapshot.service_id}），当前 {current.name}（{current.service_id}）"
        )
    if current.amount_usd != snapshot.amount_usd:
        return (
            "配送运费发生变化：原锁定 "
            f"{format_usd(snapshot.amount_usd)}，当前 {format_usd(current.amount_usd)}"
        )
    return None


def _form_ready(form: FormState | None) -> str | None:
    if form is None:
        return "收货/付款表单状态无法确认"
    if form.errors:
        return "官网表单提示错误：" + "；".join(form.errors)
    if not form.contact_complete:
        return "收货资料尚未完整填写"
    if not form.card_complete:
        return "信用卡资料尚未完整填写"
    return None


def evaluate_checkout(
    checkout: CheckoutRead,
    preset: ProductPreset,
    order_limit_usd: Decimal,
) -> Readiness:
    if checkout.status is not ReadStatus.OK:
        return Readiness(PreparedLevel.NONE, (_read_failure(checkout.status),))
    content = match_single_target(checkout.lines, preset, currency=checkout.currency)
    if not content.ok:
        return Readiness(PreparedLevel.NONE, (content.reason or "订单内容不符合唯一目标",))
    form_reason = _form_ready(checkout.form)
    if form_reason:
        return Readiness(PreparedLevel.NONE, (form_reason,))
    if checkout.subtotal_usd is None or checkout.tax_usd is None or checkout.shipping_total_usd is None:
        return Readiness(PreparedLevel.NONE, ("订单税费或运费尚未确定",))
    if checkout.total_usd is None:
        return Readiness(PreparedLevel.NONE, ("订单最终 USD 总额尚未确定",))
    line = content.line
    assert line is not None
    if line.unit_price_usd is None:
        return Readiness(PreparedLevel.NONE, ("当前 USD 单价无法确认",))
    expected_subtotal = line.unit_price_usd * line.quantity
    if checkout.subtotal_usd != expected_subtotal:
        return Readiness(
            PreparedLevel.NONE,
            (
                f"订单小计 {format_usd(checkout.subtotal_usd)} 与 "
                f"{line.quantity} 件 × {format_usd(line.unit_price_usd)} 不一致",
            ),
        )
    shipping, shipping_reason = freeze_shipping(checkout)
    if shipping is None:
        return Readiness(PreparedLevel.NONE, (shipping_reason or "默认配送无法确认",))
    if checkout.subtotal_usd + checkout.tax_usd + checkout.shipping_total_usd != checkout.total_usd:
        return Readiness(PreparedLevel.NONE, ("订单小计、税费、运费与总额不一致",))
    if checkout.total_usd <= 0:
        return Readiness(PreparedLevel.NONE, ("订单总额无效",))
    if checkout.total_usd > order_limit_usd:
        return Readiness(
            PreparedLevel.NONE,
            (
                f"订单总额 {format_usd(checkout.total_usd)} 超过本次上限 "
                f"{format_usd(order_limit_usd)}",
            ),
        )
    return Readiness(
        PreparedLevel.CHECKOUT_READY,
        (
            "结算页已准备；开售后仍需复核会话、内容、配送和金额",
            "结算页已准备不等于已下单、已付款或已锁定库存",
        ),
        {
            "title": line.title,
            "variant": line.variant,
            "quantity": line.quantity,
            "unit_price_usd": format_usd(line.unit_price_usd or Decimal("0")),
            "subtotal_usd": format_usd(checkout.subtotal_usd),
            "tax_usd": format_usd(checkout.tax_usd),
            "shipping": shipping.to_dict(),
            "total_usd": format_usd(checkout.total_usd),
        },
    )


def verify_before_submit(
    checkout: CheckoutRead,
    preset: ProductPreset,
    order_limit_usd: Decimal,
    snapshot: ShippingSnapshot,
) -> Readiness:
    readiness = evaluate_checkout(checkout, preset, order_limit_usd)
    if readiness.level is not PreparedLevel.CHECKOUT_READY:
        return readiness
    shipping_reason = verify_shipping_snapshot(checkout, snapshot)
    if shipping_reason:
        return Readiness(PreparedLevel.NONE, (shipping_reason,))
    if not checkout.final_submit_enabled:
        return Readiness(PreparedLevel.NONE, ("最终购买按钮当前不可用",))
    return readiness


def order_matches_target(result: OrderResult, preset: ProductPreset) -> bool:
    if result.lines:
        return match_single_target(result.lines, preset, currency=USD).ok
    return result.matches_target


def _payment_state_accepted(payment_state: str | None) -> bool:
    if not payment_state:
        return False
    normalized = payment_state.casefold()
    if any(
        marker in normalized
        for marker in ("not ", "unpaid", "pending", "processing", "failed", "declined", "rejected", "void")
    ):
        return False
    return any(token in normalized for token in _ACCEPTED_PAYMENT_STATES)


def _payment_state_declined(payment_state: str | None) -> bool:
    if not payment_state:
        return False
    normalized = payment_state.casefold()
    return any(token in normalized for token in _DECLINED_PAYMENT_STATES)


def classify_order_result(
    result: OrderResult,
    preset: ProductPreset,
) -> tuple[SubmissionState, str]:
    if result.status is OrderStatus.DECLINED or _payment_state_declined(result.payment_state):
        return SubmissionState.DECLINED, "官网明确表示付款被拒绝"
    if result.status is OrderStatus.CONFIRMED:
        if not result.order_id:
            return SubmissionState.UNKNOWN, "官网显示订单确认但无法取得订单标识"
        if not order_matches_target(result, preset):
            return SubmissionState.UNKNOWN, "订单内容无法关联到本次唯一目标"
        if not _payment_state_accepted(result.payment_state):
            return SubmissionState.UNKNOWN, "付款未被明确接受，结果仍需核对"
        return SubmissionState.SUCCEEDED, f"订单 {result.order_id} 已确认，付款状态 {result.payment_state}"
    if result.status is OrderStatus.PENDING:
        return SubmissionState.UNKNOWN, "官网仍在处理付款"
    return SubmissionState.UNKNOWN, result.evidence or "无法确认订单或付款结果"


def build_submission_record(
    *,
    task_id: str,
    preset: ProductPreset,
    order_limit_usd: Decimal,
    prepared_level: PreparedLevel,
    planned_start_utc: datetime | None,
    deadline_utc: datetime,
    shipping: ShippingSnapshot,
    total_usd: Decimal,
    now: datetime,
) -> SubmissionRecord:
    return SubmissionRecord(
        schema_version=SUBMISSION_SCHEMA_VERSION,
        task_id=task_id,
        state=SubmissionState.SUBMITTING,
        created_at_utc=utc_now_iso(now),
        updated_at_utc=utc_now_iso(now),
        prepared_level=prepared_level,
        planned_start_utc=planned_start_utc.astimezone(timezone.utc).isoformat() if planned_start_utc else None,
        deadline_utc=deadline_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
        target_title=preset.title,
        target_variant=preset.variant,
        target_quantity=preset.quantity,
        unit_price_limit_usd=f"{preset.unit_price_limit_usd:.2f}",
        order_limit_usd=f"{order_limit_usd:.2f}",
        shipping_service_id=shipping.service_id,
        shipping_service_name=shipping.name,
        shipping_amount_usd=f"{shipping.amount_usd:.2f}",
        total_usd=f"{total_usd:.2f}",
    )
