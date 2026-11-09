from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Callable, Protocol

from .browser import PurchaseSession, TrustedSourcePolicy, ensure_trusted_payment_origin
from .models import (
    ActiveWindow,
    AddToCartState,
    BEIJING_TZ,
    CheckoutRead,
    LoginRead,
    LoginStatus,
    OrderResult,
    OrderStatus,
    PageKind,
    PreparedLevel,
    ProductPageState,
    ReadStatus,
    Readiness,
    ScheduleMode,
    ScheduleSettings,
    ShippingSnapshot,
    SubmissionState,
    TaskEvent,
    TaskPhase,
    TaskSnapshot,
    UTC,
    format_beijing_datetime,
    format_usd,
    normalize_text,
)
from .purchase import (
    SubmissionBlocked,
    SubmissionLoadStatus,
    SubmissionRecord,
    SubmissionStore,
    build_submission_record,
    classify_order_result,
    evaluate_cart,
    evaluate_checkout,
    freeze_shipping,
    match_single_target,
    verify_before_submit,
    verify_shipping_snapshot,
)
from .state import (
    AddToCartRecord,
    RunStateBlocked,
    RunStateStatus,
    RunStateStore,
    build_add_to_cart_record,
)
from .redaction import Redactor

PREPARATION_TIMEOUT_SECONDS = 30 * 60.0
MANUAL_LOGIN_TIMEOUT_SECONDS = 10 * 60.0
LOGIN_RESULT_TIMEOUT_SECONDS = 30.0
RESULT_TIMEOUT_SECONDS = 10 * 60.0
POLL_INTERVAL_SECONDS = 0.1
RESULT_POLL_INTERVAL_SECONDS = 2.0
CART_REFRESH_MIN_INTERVAL_SECONDS = 1.0
SOLD_OUT_INTERVAL_SECONDS = 15.0
TRANSIENT_RETRY_INTERVAL_SECONDS = 10.0
READ_ERROR_LIMIT = 3
STEP_TIMEOUT_SECONDS = 30.0


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class WindowSummary:
    mode: ScheduleMode
    window_seconds: int
    started: bool
    started_at_utc: datetime | None
    planned_start_utc: datetime | None
    deadline_utc: datetime | None
    remaining_seconds: float | None
    expired: bool
    text: str


def describe_window(
    schedule: ScheduleSettings,
    *,
    now: datetime,
    window: ActiveWindow | None = None,
    ready: bool = False,
) -> WindowSummary:
    mode = schedule.mode
    if window is not None:
        if window.deadline_utc is None:
            elapsed = max(0.0, (now - window.started_at_utc).total_seconds())
            return WindowSummary(
                mode=mode,
                window_seconds=schedule.window_seconds,
                started=True,
                started_at_utc=window.started_at_utc,
                planned_start_utc=None,
                deadline_utc=None,
                remaining_seconds=None,
                expired=False,
                text=f"已运行 {int(elapsed)} 秒；无整单截止，按步骤超时和 Stop 控制",
            )
        remaining = window.remaining_seconds(now)
        expired = window.expired(now)
        text = (
            f"实际起点 {format_beijing_datetime(window.effective_start_utc)}（北京时间 UTC+08:00）；"
            f"提交截止 {format_beijing_datetime(window.deadline_utc)}；"
            f"剩余 {int(remaining)} 秒"
        )
        if expired:
            text = (
                f"提交窗口已于 {format_beijing_datetime(window.deadline_utc)} 结束；"
                "未提交则不再自动付款，已提交只核对结果"
            )
        return WindowSummary(
            mode=mode,
            window_seconds=schedule.window_seconds,
            started=True,
            started_at_utc=window.started_at_utc,
            planned_start_utc=schedule.scheduled_start_utc,
            deadline_utc=window.deadline_utc,
            remaining_seconds=remaining,
            expired=expired,
            text=text,
        )
    if mode is ScheduleMode.SCHEDULED and schedule.scheduled_start_utc is not None:
        deadline = schedule.planned_deadline_utc()
        expired = deadline is not None and now >= deadline
        if expired:
            text = (
                "计划提交窗口已过期；不会自动补跑或延期。"
                f"计划起点 {format_beijing_datetime(schedule.scheduled_start_utc)}，"
                f"计划截止 {format_beijing_datetime(deadline)}"
            )
        else:
            ready_text = "已就绪，等待计划起点" if ready else "到点前需完成准备；到点未就绪将停止"
            text = (
                f"计划起点 {format_beijing_datetime(schedule.scheduled_start_utc)}（北京时间 UTC+08:00）；"
                f"计划提交截止 {format_beijing_datetime(deadline)}；{ready_text}"
            )
        return WindowSummary(
            mode=mode,
            window_seconds=schedule.window_seconds,
            started=False,
            started_at_utc=None,
            planned_start_utc=schedule.scheduled_start_utc,
            deadline_utc=deadline,
            remaining_seconds=max(0.0, (deadline - now).total_seconds()) if deadline else None,
            expired=expired,
            text=text,
        )
    text = "无整单截止；准备就绪后开始，按步骤超时和 Stop 控制"
    if ready:
        text = "准备已就绪；将立即开始完整流程，无整单倒计时"
    return WindowSummary(
        mode=mode,
        window_seconds=schedule.window_seconds,
        started=False,
        started_at_utc=None,
        planned_start_utc=None,
        deadline_utc=None,
        remaining_seconds=None,
        expired=False,
        text=text,
    )


@dataclass(frozen=True)
class PerformanceSample:
    path: PreparedLevel
    action: str
    observed_at_utc: datetime
    action_at_utc: datetime
    client_ms: float


@dataclass(frozen=True)
class LoginPreparation:
    mode: str
    started_at_utc: datetime
    finished_at_utc: datetime
    elapsed_seconds: float


@dataclass
class TaskOutcome:
    phase: TaskPhase
    message: str
    prepared_level: PreparedLevel = PreparedLevel.NONE
    window: ActiveWindow | None = None
    record: SubmissionRecord | None = None
    events: list[TaskEvent] = field(default_factory=list)
    performance: list[PerformanceSample] = field(default_factory=list)
    login_preparation: LoginPreparation | None = None


@dataclass(frozen=True)
class _Prepared:
    level: PreparedLevel
    shipping: ShippingSnapshot | None
    total_usd: Decimal | None
    ready_at: datetime


class _RetryAdd(Exception):
    def __init__(self, message: str, *, interval: float) -> None:
        super().__init__(message)
        self.message = message
        self.interval = interval


class _StopTask(Exception):
    def __init__(self, phase: TaskPhase, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.phase = phase
        self.message = message
        self.unknown = unknown


class PurchaseRunner:
    def __init__(
        self,
        *,
        snapshot: TaskSnapshot,
        session: PurchaseSession,
        store: SubmissionStore,
        clock: Clock,
        events: "queue.Queue[TaskEvent]",
        cancel_event: threading.Event,
        policy: TrustedSourcePolicy,
        preparation_timeout_seconds: float = PREPARATION_TIMEOUT_SECONDS,
        result_timeout_seconds: float = RESULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        result_poll_interval_seconds: float = RESULT_POLL_INTERVAL_SECONDS,
        allow_cart_refresh: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.session = session
        self.store = store
        self.clock = clock
        self.events = events
        self.cancel_event = cancel_event
        self.policy = policy
        self.preparation_timeout_seconds = preparation_timeout_seconds
        self.result_timeout_seconds = result_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.result_poll_interval_seconds = result_poll_interval_seconds
        self.allow_cart_refresh = allow_cart_refresh
        self.events_log: list[TaskEvent] = []
        self.performance: list[PerformanceSample] = []
        self.redactor = Redactor.from_credentials(snapshot.customer, snapshot.card, snapshot.login)
        self._last_refresh_monotonic: float | None = None

    def _emit(self, phase: TaskPhase, message: str, details: dict | None = None) -> None:
        event = TaskEvent(
            at_utc=self.clock.now(),
            phase=phase,
            message=self.redactor.redact_text(message),
            details=self.redactor.redact_value(details or {}),
        )
        self.events_log.append(event)
        self.events.put(event)

    def _check_stop(self) -> None:
        if self.cancel_event.is_set():
            raise _StopTask(TaskPhase.STOPPED, "已收到停止请求，未再安排新的官网动作")

    def _sleep(self, seconds: float | None = None) -> None:
        self.clock.sleep(self.poll_interval_seconds if seconds is None else seconds)

    def _store_load_and_block(self) -> None:
        loaded = self.store.load()
        if loaded.blocking:
            reason = loaded.error or (
                f"存在未决购买提交（{loaded.record.state}），必须先核对官方订单结果"
                if loaded.record
                else "提交记录无法读取"
            )
            raise _StopTask(TaskPhase.BLOCKED, reason)

    def run(self) -> TaskOutcome:
        window: ActiveWindow | None = None
        try:
            self._store_load_and_block()
            self._emit(
                TaskPhase.PREPARING,
                "开始准备；请在专用可见 Chrome 会话中登录并手动准备目标购物车或结算页",
                {"task_id": self.snapshot.task_id},
            )
            prepared = self._prepare()
            window = self._open_window(prepared)
            self._emit(
                TaskPhase.WAITING_WINDOW,
                "提交窗口已开启；仅在截止前允许一次最终购买提交",
                {"deadline_utc": window.deadline_utc.isoformat()},
            )
            outcome = self._execute_window(prepared, window)
            if outcome.window is None:
                outcome.window = window
            return outcome
        except _StopTask as stop:
            self._emit(stop.phase, stop.message)
            return TaskOutcome(
                phase=stop.phase,
                message=stop.message,
                window=window,
                events=list(self.events_log),
                performance=list(self.performance),
            )
        except Exception as exc:  # noqa: BLE001 - never let the worker die silently
            message = f"任务异常停止：{exc}"
            self._emit(TaskPhase.FAILED, message)
            return TaskOutcome(
                phase=TaskPhase.FAILED,
                message=self.redactor.redact_text(message),
                window=window,
                events=list(self.events_log),
                performance=list(self.performance),
            )

    def _preparation_deadline(self, prepared: _Prepared | None) -> datetime | None:
        return None

    def _prepare(self) -> _Prepared:
        schedule = self.snapshot.schedule
        now = self.clock.now()
        if schedule.mode is ScheduleMode.SCHEDULED:
            if schedule.scheduled_start_utc is None:
                raise _StopTask(TaskPhase.MISSED, "定时模式缺少开售时间")
            deadline = schedule.planned_deadline_utc()
            if deadline is None or now >= deadline:
                raise _StopTask(TaskPhase.MISSED, "计划提交窗口已经过期，不会补跑或延期")
            preparation_deadline = schedule.scheduled_start_utc
        else:
            preparation_deadline = now + timedelta(seconds=self.preparation_timeout_seconds)

        last_reason: str | None = None
        while True:
            self._check_stop()
            now = self.clock.now()
            if now >= preparation_deadline:
                if schedule.mode is ScheduleMode.SCHEDULED:
                    raise _StopTask(TaskPhase.MISSED, "到达计划起点时尚未完成准备，不会自动晚启动")
                raise _StopTask(TaskPhase.MISSED, "准备等待已达到时间上限，任务未开始")
            try:
                kind = self.session.page_kind()
            except Exception as exc:  # noqa: BLE001 - browser may be closed
                raise _StopTask(TaskPhase.STOPPED, f"无法读取浏览器页面：{exc}") from exc
            if kind is PageKind.LOGIN:
                self._emit_once(last_reason, TaskPhase.AWAITING_LOGIN, "请在专用浏览器会话中手动登录")
                last_reason = "请登录"
                self._sleep()
                continue
            if kind is PageKind.BLOCKED:
                raise _StopTask(TaskPhase.UNSUPPORTED, "官网访问受限，不能继续准备；未尝试绕过限制")
            if kind is PageKind.CART:
                cart = self.session.read_cart()
                if cart.status in (ReadStatus.SESSION_LOST, ReadStatus.NOT_LOGGED_IN):
                    self._emit_once(last_reason, TaskPhase.AWAITING_LOGIN, "登录状态尚未确认，请在同一会话中登录")
                    last_reason = "请登录"
                    self._sleep()
                    continue
                if cart.status is ReadStatus.BLOCKED:
                    raise _StopTask(TaskPhase.UNSUPPORTED, "购物车页面访问受限，不能继续准备")
                readiness = evaluate_cart(cart, self.snapshot.target.preset)
                if readiness.level is PreparedLevel.CART_READY:
                    self._emit(TaskPhase.CART_READY, "购物车已准备", readiness.details)
                    return _Prepared(PreparedLevel.CART_READY, None, None, now)
                reason = "；".join(readiness.reasons) or "购物车尚未准备"
                self._emit_once(last_reason, TaskPhase.PREPARING, reason)
                last_reason = reason
                self._sleep()
                continue
            if kind is PageKind.CHECKOUT:
                checkout = self.session.read_checkout()
                if checkout.status in (ReadStatus.SESSION_LOST, ReadStatus.NOT_LOGGED_IN):
                    self._emit_once(last_reason, TaskPhase.AWAITING_LOGIN, "结算会话需要重新登录")
                    last_reason = "请登录"
                    self._sleep()
                    continue
                if checkout.status is ReadStatus.BLOCKED:
                    raise _StopTask(TaskPhase.UNSUPPORTED, "结算页面访问受限，不能继续准备")
                readiness = evaluate_checkout(checkout, self.snapshot.target.preset, self.snapshot.target.order_limit_usd)
                if readiness.level is PreparedLevel.CHECKOUT_READY:
                    shipping, shipping_reason = freeze_shipping(checkout)
                    if shipping is None:
                        raise _StopTask(TaskPhase.FAILED, shipping_reason or "默认配送无法确认")
                    self._emit(TaskPhase.CHECKOUT_READY, "结算页已准备", readiness.details)
                    return _Prepared(PreparedLevel.CHECKOUT_READY, shipping, checkout.total_usd, now)
                form_reason = self._form_reason(checkout)
                if form_reason is not None:
                    result = self.session.fill_checkout(self.snapshot.customer, self.snapshot.card, self.policy)
                    if not result.ok:
                        raise _StopTask(
                            TaskPhase.FAILED,
                            "结算资料无法安全填写：" + (result.error or "未知错误"),
                        )
                    checkout = self.session.read_checkout()
                    readiness = evaluate_checkout(
                        checkout,
                        self.snapshot.target.preset,
                        self.snapshot.target.order_limit_usd,
                    )
                    if readiness.level is PreparedLevel.CHECKOUT_READY:
                        shipping, shipping_reason = freeze_shipping(checkout)
                        if shipping is None:
                            raise _StopTask(TaskPhase.FAILED, shipping_reason or "默认配送无法确认")
                        self._emit(TaskPhase.CHECKOUT_READY, "结算页已准备", readiness.details)
                        return _Prepared(PreparedLevel.CHECKOUT_READY, shipping, checkout.total_usd, now)
                reason = "；".join(readiness.reasons) or "结算页尚未准备"
                self._emit_once(last_reason, TaskPhase.PREPARING, reason)
                last_reason = reason
                self._sleep()
                continue
            self._emit_once(last_reason, TaskPhase.PREPARING, "请在专用浏览器会话中打开目标购物车或结算页")
            last_reason = "请打开目标页面"
            self._sleep()

    def _emit_once(self, last_reason: str | None, phase: TaskPhase, message: str) -> None:
        if last_reason != message:
            self._emit(phase, message)

    def _form_reason(self, checkout: CheckoutRead) -> str | None:
        if checkout.form is None:
            return None
        if checkout.form.errors or not checkout.form.contact_complete or not checkout.form.card_complete:
            return "表单待填写"
        return None

    def _open_window(self, prepared: _Prepared) -> ActiveWindow:
        schedule = self.snapshot.schedule
        now = self.clock.now()
        if schedule.mode is ScheduleMode.SCHEDULED:
            planned = schedule.scheduled_start_utc
            assert planned is not None
            deadline = schedule.planned_deadline_utc()
            assert deadline is not None
            if prepared.ready_at >= planned:
                raise _StopTask(TaskPhase.MISSED, "达到计划起点时尚未完成准备，不会自动晚启动")
            if now < planned:
                while self.clock.now() < planned:
                    self._check_stop()
                    self._sleep(min(self.poll_interval_seconds, max(0.0, (planned - self.clock.now()).total_seconds())))
                now = self.clock.now()
            if now >= deadline:
                raise _StopTask(TaskPhase.EXPIRED, "唤醒时已超过原提交截止，不会补跑")
            return ActiveWindow(
                mode=ScheduleMode.SCHEDULED,
                started_at_utc=now,
                effective_start_utc=planned,
                deadline_utc=deadline,
            )
        return ActiveWindow(
            mode=ScheduleMode.IMMEDIATE,
            started_at_utc=now,
            effective_start_utc=now,
            deadline_utc=now + timedelta(seconds=schedule.window_seconds),
        )

    def _execute_window(self, prepared: _Prepared, window: ActiveWindow) -> TaskOutcome:
        level = prepared.level
        start_level = prepared.level
        shipping = prepared.shipping
        entered_checkout = level is PreparedLevel.CHECKOUT_READY
        last_reason: str | None = None
        while True:
            self._check_stop()
            now = self.clock.now()
            if now >= window.deadline_utc:
                raise _StopTask(
                    TaskPhase.EXPIRED,
                    "提交窗口已截止；截止前未提交购买，不再发起新的付款动作",
                )
            if not entered_checkout:
                cart = self.session.read_cart()
                if cart.status in (ReadStatus.SESSION_LOST, ReadStatus.NOT_LOGGED_IN):
                    raise _StopTask(TaskPhase.STOPPED, "已准备会话失效，停止自动流程并保留浏览器")
                if cart.status is ReadStatus.BLOCKED:
                    raise _StopTask(TaskPhase.UNSUPPORTED, "官网访问受限，停止自动流程")
                observed = self.clock.now()
                observed_perf = time.perf_counter()
                readiness = evaluate_cart(cart, self.snapshot.target.preset)
                if readiness.level is not PreparedLevel.CART_READY:
                    if cart.status is ReadStatus.ERROR:
                        self._bounded_read_wait(window, last_reason, "购物车读取暂时失败，窗口内有限重试")
                        continue
                    raise _StopTask(
                        TaskPhase.STOPPED,
                        "购物车内容已不再匹配唯一目标：" + ("；".join(readiness.reasons) or "无法确认"),
                    )
                action_perf = time.perf_counter()
                entered = self.session.enter_checkout()
                if entered.attempted or entered.ok or entered.uncertain:
                    self._record_performance(
                        PreparedLevel.CART_READY,
                        "enter_checkout",
                        observed,
                        observed_perf,
                        action_perf,
                    )
                if entered.uncertain:
                    raise _StopTask(
                        TaskPhase.STOPPED,
                        "去结算动作结果不明，未重复导航；请保留浏览器人工核对",
                    )
                if entered.ok:
                    entered_checkout = True
                    self._emit(TaskPhase.CHECKING_OUT, "已沿官方入口进入结算，继续核对订单和表单")
                    continue
                self._emit_once(last_reason, TaskPhase.CHECKING_OUT, "官方结算入口尚未开放，窗口内继续观察")
                last_reason = "等待结算入口"
                self._maybe_refresh_cart()
                self._sleep()
                continue

            checkout = self.session.read_checkout()
            if checkout.status in (ReadStatus.SESSION_LOST, ReadStatus.NOT_LOGGED_IN):
                raise _StopTask(TaskPhase.STOPPED, "结算会话失效，停止自动流程并保留浏览器")
            if checkout.status is ReadStatus.BLOCKED:
                raise _StopTask(TaskPhase.UNSUPPORTED, "结算页面访问受限，停止自动流程")
            if checkout.status in (ReadStatus.ERROR, ReadStatus.UNRECOGNIZED, ReadStatus.NOT_AVAILABLE):
                read_failures += 1
                if read_failures >= READ_ERROR_LIMIT:
                    raise _StopTask(TaskPhase.STOPPED, "结算页连续读取异常 3 次，停止并保留浏览器")
                self._emit_once(last_reason, TaskPhase.REVALIDATING, "结算页暂时无法读取，有限只读重试")
                last_reason = "等待结算页"
                self._sleep()
                continue
            read_failures = 0
            form_reason = self._form_reason(checkout)
            if form_reason is not None:
                result = self.session.fill_checkout(self.snapshot.customer, self.snapshot.card, self.policy)
                if not result.ok:
                    raise _StopTask(TaskPhase.FAILED, "结算资料填写失败：" + (result.error or "未知错误"))
                checkout = self.session.read_checkout()
            content = match_single_target(
                checkout.lines,
                self.snapshot.target.preset,
                currency=checkout.currency,
            )
            if not content.ok:
                raise _StopTask(
                    TaskPhase.STOPPED,
                    "结算订单摘要已不再匹配唯一目标：" + (content.reason or "无法确认"),
                )
            readiness = evaluate_checkout(
                checkout,
                self.snapshot.target.preset,
                self.snapshot.target.order_limit_usd,
            )
            if readiness.level is not PreparedLevel.CHECKOUT_READY:
                if self._is_transient_checkout_gap(checkout):
                    self._emit_once(last_reason, TaskPhase.REVALIDATING, "等待官方报价或入口开放")
                    last_reason = "等待报价"
                    self._sleep()
                    continue
                raise _StopTask(
                    TaskPhase.STOPPED,
                    "结算内容已不再通过核对：" + ("；".join(readiness.reasons) or "无法确认"),
                )
            if checkout.payment_origin is not None:
                source = ensure_trusted_payment_origin(self.policy, checkout.payment_origin)
                if not source.ok:
                    raise _StopTask(TaskPhase.FAILED, "支付页面来源未核实，停止付款")
            if shipping is None:
                shipping, shipping_reason = freeze_shipping(checkout)
                if shipping is None:
                    raise _StopTask(TaskPhase.FAILED, shipping_reason or "默认配送无法确认")
                assert checkout.total_usd is not None
                prepared = _Prepared(
                    level=PreparedLevel.CHECKOUT_READY,
                    shipping=shipping,
                    total_usd=checkout.total_usd,
                    ready_at=self.clock.now(),
                )
            else:
                shipping_reason = verify_shipping_snapshot(checkout, shipping)
                if shipping_reason:
                    raise _StopTask(TaskPhase.STOPPED, shipping_reason)
            if not checkout.final_submit_enabled:
                self._emit_once(last_reason, TaskPhase.REVALIDATING, "最终购买按钮尚未开放，窗口内继续观察")
                last_reason = "等待最终提交"
                self._sleep()
                continue
            observed = self.clock.now()
            observed_perf = time.perf_counter()
            final_readiness = verify_before_submit(
                checkout,
                self.snapshot.target.preset,
                self.snapshot.target.order_limit_usd,
                shipping,
            )
            if final_readiness.level is not PreparedLevel.CHECKOUT_READY:
                reason = "；".join(final_readiness.reasons) or "付款前复核未通过"
                raise _StopTask(TaskPhase.STOPPED, reason)
            outcome = self._submit_once(window, prepared, shipping, observed, observed_perf, start_level)
            if outcome is not None:
                return outcome

    def _is_transient_checkout_gap(self, checkout: CheckoutRead) -> bool:
        if checkout.form is None:
            return True
        if checkout.subtotal_usd is None or checkout.tax_usd is None or checkout.shipping_total_usd is None:
            return True
        if checkout.total_usd is None:
            return True
        return False

    def _maybe_refresh_cart(self) -> None:
        if not self.allow_cart_refresh:
            return
        now = time.monotonic()
        if self._last_refresh_monotonic is not None and now - self._last_refresh_monotonic < CART_REFRESH_MIN_INTERVAL_SECONDS:
            return
        self._last_refresh_monotonic = now
        try:
            self.session.refresh()
        except Exception:  # noqa: BLE001 - refresh is best effort; original state remains
            pass

    def _bounded_read_wait(self, window: ActiveWindow, last_reason: str | None, message: str) -> None:
        self._emit_once(last_reason, TaskPhase.REVALIDATING, message)
        self._sleep()

    def _record_performance(
        self,
        path: PreparedLevel,
        action: str,
        observed: datetime,
        observed_perf: float | None = None,
        action_perf: float | None = None,
    ) -> None:
        now = self.clock.now()
        if observed_perf is not None and action_perf is not None:
            client_ms = (action_perf - observed_perf) * 1000.0
        else:
            client_ms = (now - observed).total_seconds() * 1000.0
        self.performance.append(
            PerformanceSample(
                path=path,
                action=action,
                observed_at_utc=observed,
                action_at_utc=now,
                client_ms=max(0.0, client_ms),
            )
        )

    def _submit_once(
        self,
        window: ActiveWindow,
        prepared: _Prepared,
        shipping: ShippingSnapshot,
        observed: datetime,
        observed_perf: float,
        start_level: PreparedLevel,
    ) -> TaskOutcome | None:
        now = self.clock.now()
        if now >= window.deadline_utc:
            raise _StopTask(TaskPhase.EXPIRED, "最终提交前已超过截止，不发起付款")
        self._check_stop()
        assert prepared.total_usd is not None
        record = build_submission_record(
            task_id=self.snapshot.task_id,
            preset=self.snapshot.target.preset,
            order_limit_usd=self.snapshot.target.order_limit_usd,
            prepared_level=start_level,
            planned_start_utc=self.snapshot.schedule.scheduled_start_utc,
            deadline_utc=now + timedelta(seconds=self.result_timeout_seconds),
            shipping=shipping,
            total_usd=prepared.total_usd,
            now=now,
        )
        try:
            self.store.begin(record)
        except SubmissionBlocked as exc:
            raise _StopTask(TaskPhase.BLOCKED, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never click without a durable record
            raise _StopTask(TaskPhase.FAILED, f"无法保存购买提交状态，未触发付款：{exc}") from exc
        self._emit(TaskPhase.SUBMITTING, "最终核对通过，已记录提交状态，准备发起一次官方购买提交")
        if self.cancel_event.is_set():
            try:
                self._mark_not_submitted(record, "提交记录保存后、实际点击前收到停止请求")
            except Exception:  # noqa: BLE001 - keep blocking record
                pass
            raise _StopTask(TaskPhase.STOPPED, "收到停止请求，未触发付款")
        action_perf = time.perf_counter()
        try:
            result = self.session.submit_purchase()
        except Exception as exc:  # noqa: BLE001 - unknown whether the click reached the site
            self._record_performance(
                start_level,
                "submit_purchase",
                observed,
                observed_perf,
                action_perf,
            )
            record = self._update_record(record, state=SubmissionState.UNKNOWN, note=f"提交动作异常：{exc}")
            self._emit(TaskPhase.AWAITING_VERIFICATION, "付款提交结果不明，禁止重复提交；请人工核对")
            return self._await_result(record)
        if result.attempted or result.sent:
            self._record_performance(
                start_level,
                "submit_purchase",
                observed,
                observed_perf,
                action_perf,
            )
        if not result.attempted and result.sent is False:
            try:
                self._mark_not_submitted(record, result.error or "最终按钮在点击前不可用")
            except Exception as exc:  # noqa: BLE001
                raise _StopTask(TaskPhase.UNKNOWN, f"未能记录未提交结束状态：{exc}") from exc
            raise _StopTask(TaskPhase.NOT_SUBMITTED, result.error or "最终购买未被发出")
        if result.error:
            record = self._update_record(record, state=SubmissionState.UNKNOWN, note=result.error)
        self._emit(TaskPhase.AWAITING_VERIFICATION, "购买提交已发出；仅核对结果，不再重复提交")
        return self._await_result(record)

    def _mark_not_submitted(self, record: SubmissionRecord, note: str) -> None:
        updated = self._update_record(record, state=SubmissionState.NOT_SUBMITTED, note=note)
        self._emit(TaskPhase.NOT_SUBMITTED, note, {"state": str(updated.state)})

    def _update_record(self, record: SubmissionRecord, **changes) -> SubmissionRecord:
        from dataclasses import replace

        updated = replace(record, updated_at_utc=self.clock.now().astimezone(UTC).isoformat(timespec="seconds"), **changes)
        self.store._write(updated)
        return updated

    def _await_result(self, record: SubmissionRecord) -> TaskOutcome:
        deadline = self.clock.now() + timedelta(seconds=self.result_timeout_seconds)
        while self.clock.now() < deadline:
            if self.cancel_event.is_set():
                try:
                    record = self._update_record(
                        record,
                        state=SubmissionState.UNKNOWN,
                        note="用户停止；自动化已停止，结果仍需在官方渠道核对",
                    )
                except Exception:  # noqa: BLE001 - keep original blocking record
                    pass
                raise _StopTask(
                    TaskPhase.STOPPED,
                    "已停止自动操作；购买提交可能已发出，请人工核对官方订单，禁止直接重试",
                )
            try:
                result = self.session.read_order_result()
            except Exception as exc:  # noqa: BLE001
                result = OrderResult(status=OrderStatus.UNKNOWN, evidence=str(exc))
            state, note = classify_order_result(result, self.snapshot.target.preset)
            if state is SubmissionState.SUCCEEDED:
                record = self._update_record(
                    record,
                    state=state,
                    outcome=state.value,
                    order_id=result.order_id,
                    payment_state=result.payment_state,
                    note=note,
                )
                self._emit(TaskPhase.SUCCEEDED, note, {"order_id": result.order_id})
                return TaskOutcome(
                    phase=TaskPhase.SUCCEEDED,
                    message=note,
                    prepared_level=PreparedLevel.CHECKOUT_READY,
                    record=record,
                    events=list(self.events_log),
                    performance=list(self.performance),
                )
            if state is SubmissionState.DECLINED:
                record = self._update_record(
                    record,
                    state=state,
                    outcome=state.value,
                    order_id=result.order_id,
                    payment_state=result.payment_state,
                    note=note,
                )
                self._emit(TaskPhase.FAILED, note)
                return TaskOutcome(
                    phase=TaskPhase.FAILED,
                    message=note,
                    prepared_level=PreparedLevel.CHECKOUT_READY,
                    record=record,
                    events=list(self.events_log),
                    performance=list(self.performance),
                )
            self._sleep(self.result_poll_interval_seconds)
        record = self._update_record(
            record,
            state=SubmissionState.UNKNOWN,
            note="结果等待期限结束仍未确认；请人工核对官方订单",
        )
        self._emit(TaskPhase.UNKNOWN, "结果等待期限结束仍未确认；请人工核对官方订单")
        return TaskOutcome(
            phase=TaskPhase.UNKNOWN,
            message="购买提交结果不明；禁止自动重复提交",
            prepared_level=PreparedLevel.CHECKOUT_READY,
            record=record,
            events=list(self.events_log),
            performance=list(self.performance),
        )



class FullFlowRunner:
    """Baseline flow: empty cart -> product page -> one add -> checkout -> one submit."""

    def __init__(
        self,
        *,
        snapshot: TaskSnapshot,
        session,
        store: RunStateStore,
        clock: Clock,
        events: "queue.Queue[TaskEvent]",
        cancel_event: threading.Event,
        policy: TrustedSourcePolicy,
        preparation_timeout_seconds: float = PREPARATION_TIMEOUT_SECONDS,
        result_timeout_seconds: float = RESULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        result_poll_interval_seconds: float = RESULT_POLL_INTERVAL_SECONDS,
        allow_product_refresh: bool = False,
        manual_login_timeout_seconds: float = MANUAL_LOGIN_TIMEOUT_SECONDS,
        login_result_timeout_seconds: float = LOGIN_RESULT_TIMEOUT_SECONDS,
    ) -> None:
        self.snapshot = snapshot
        self.session = session
        self.store = store
        self.clock = clock
        self.events = events
        self.cancel_event = cancel_event
        self.policy = policy
        self.preparation_timeout_seconds = preparation_timeout_seconds
        self.result_timeout_seconds = result_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.result_poll_interval_seconds = result_poll_interval_seconds
        self.allow_product_refresh = allow_product_refresh
        self.manual_login_timeout_seconds = manual_login_timeout_seconds
        self.login_result_timeout_seconds = login_result_timeout_seconds
        self.events_log: list[TaskEvent] = []
        self.performance: list[PerformanceSample] = []
        self.redactor = Redactor.from_credentials(snapshot.customer, snapshot.card, snapshot.login)
        self._last_refresh_monotonic: float | None = None
        self._login_preparation: LoginPreparation | None = None

    def _emit(self, phase: TaskPhase, message: str, details: dict | None = None) -> None:
        event = TaskEvent(
            at_utc=self.clock.now(),
            phase=phase,
            message=self.redactor.redact_text(message),
            details=self.redactor.redact_value(details or {}),
        )
        self.events_log.append(event)
        self.events.put(event)

    def _check_stop(self) -> None:
        if self.cancel_event.is_set():
            raise _StopTask(TaskPhase.STOPPED, "已收到停止请求，未再安排新的官网动作")

    def _sleep(self, seconds: float | None = None) -> None:
        self.clock.sleep(self.poll_interval_seconds if seconds is None else seconds)

    def _record_performance(
        self,
        path: PreparedLevel,
        action: str,
        observed: datetime,
        observed_perf: float | None = None,
        action_perf: float | None = None,
    ) -> None:
        now = self.clock.now()
        if observed_perf is not None and action_perf is not None:
            client_ms = (action_perf - observed_perf) * 1000.0
        else:
            client_ms = (now - observed).total_seconds() * 1000.0
        self.performance.append(
            PerformanceSample(
                path=path,
                action=action,
                observed_at_utc=observed,
                action_at_utc=now,
                client_ms=max(0.0, client_ms),
            )
        )

    def _check_run_state(self) -> None:
        loaded = self.store.load()
        if loaded.blocking:
            detail = loaded.error
            if detail is None and loaded.add_to_cart is not None:
                detail = f"存在未决加购（{loaded.add_to_cart.state}）"
            if detail is None and loaded.purchase is not None:
                detail = f"存在未决购买提交（{loaded.purchase.state}）"
            raise _StopTask(TaskPhase.BLOCKED, detail or "存在未决记录，必须先核对")

    def run(self) -> TaskOutcome:
        window: ActiveWindow | None = None
        try:
            self._check_run_state()
            self._emit(
                TaskPhase.PREPARING,
                "开始基础准备：检查专用会话登录、配置与空购物车；不会自动清车",
                {"task_id": self.snapshot.task_id},
            )
            window = self._prepare_run_ready()
            self._emit(
                TaskPhase.RUN_READY,
                "运行已就绪；立即从商品页开始完整流程，无整单截止",
                {"started_at_utc": window.started_at_utc.isoformat()},
            )
            outcome = self._execute_full_flow(window)
            outcome.window = window
            outcome.login_preparation = self._login_preparation
            return outcome
        except _StopTask as stop:
            self._emit(stop.phase, stop.message)
            return TaskOutcome(
                phase=stop.phase,
                message=stop.message,
                window=window,
                events=list(self.events_log),
                performance=list(self.performance),
                login_preparation=self._login_preparation,
            )
        except Exception as exc:  # noqa: BLE001 - never let the worker die silently
            message = f"任务异常停止：{exc}"
            self._emit(TaskPhase.FAILED, message)
            return TaskOutcome(
                phase=TaskPhase.FAILED,
                message=self.redactor.redact_text(message),
                window=window,
                events=list(self.events_log),
                performance=list(self.performance),
                login_preparation=self._login_preparation,
            )

    def _prepare_run_ready(self) -> ActiveWindow:
        deadline = self.clock.now() + timedelta(seconds=self.preparation_timeout_seconds)
        login_started = self.clock.now()
        login_perf = time.perf_counter()
        login_mode = self._ensure_login_verified(deadline)
        login_finished = self.clock.now()
        self._login_preparation = LoginPreparation(
            mode=login_mode,
            started_at_utc=login_started,
            finished_at_utc=login_finished,
            elapsed_seconds=max(0.0, (login_finished - login_started).total_seconds()),
        )
        self._record_performance(
            PreparedLevel.NONE,
            f"login_prepare_{login_mode.lower()}",
            login_started,
            login_perf,
            time.perf_counter(),
        )

        navigation = self.session.navigate_to_cart()
        if navigation.uncertain:
            raise _StopTask(TaskPhase.STOPPED, "登录核实后打开购物车的结果不明，未继续自动流程")
        if not navigation.ok:
            raise _StopTask(TaskPhase.UNSUPPORTED, navigation.error or "无法打开官方购物车")
        self._assert_session_account()
        cart = self.session.read_cart()
        if cart.status is ReadStatus.NOT_LOGGED_IN or self.session.is_logged_in() is False:
            raise _StopTask(TaskPhase.STOPPED, "登录核实后会话失效；请重新开始任务，程序不会自动补登录")
        if cart.status in (ReadStatus.SESSION_LOST, ReadStatus.BLOCKED):
            raise _StopTask(TaskPhase.UNSUPPORTED, "购物车会话或访问受限，不能继续")
        if cart.status in (ReadStatus.ERROR, ReadStatus.NOT_AVAILABLE, ReadStatus.UNRECOGNIZED):
            raise _StopTask(TaskPhase.STOPPED, "登录后无法确认空购物车，未开始购买")
        if cart.status is ReadStatus.OK and cart.lines:
            raise _StopTask(
                TaskPhase.STOPPED,
                "购物车已有商品或无法确认空车；请自行清理后重新开始，程序不会自动清车",
            )
        if cart.status is not ReadStatus.EMPTY and not (cart.status is ReadStatus.OK and not cart.lines):
            raise _StopTask(TaskPhase.STOPPED, "无法确认空购物车，未执行加购")
        now = self.clock.now()
        self._emit(
            TaskPhase.RUN_READY,
            "环境、配置账号登录与空购物车检查通过",
            {"empty_cart": True, "login_mode": login_mode},
        )
        return ActiveWindow(
            mode=ScheduleMode.IMMEDIATE,
            started_at_utc=now,
            effective_start_utc=now,
            deadline_utc=None,
        )

    def _assert_session_account(self) -> None:
        state = LoginRead(status=LoginStatus.UNRECOGNIZED)
        for _ in range(5):
            state = self.session.read_login_state()
            if state.status is LoginStatus.LOGGED_IN and self._identity_matches(state.account_identity):
                return
            if state.status in (LoginStatus.BLOCKED, LoginStatus.SESSION_LOST):
                raise _StopTask(TaskPhase.UNSUPPORTED, "官网登录会话或页面访问失效，停止执行")
            if state.status not in (LoginStatus.ERROR, LoginStatus.UNRECOGNIZED):
                break
            self._check_stop()
            self._sleep()
        raise _StopTask(
            TaskPhase.STOPPED,
            "执行中登录会话失效、账号变化或身份无法核实；停止并保留浏览器，不自动补登录",
        )

    def _identity_matches(self, identity: str | None) -> bool:
        configured = normalize_text(self.snapshot.login.email)
        actual = normalize_text(identity or "")
        if not configured or not actual:
            return False
        if actual == configured:
            return True
        email_match = re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", actual)
        return bool(email_match and normalize_text(email_match.group(0)) == configured)

    def _ensure_login_verified(self, preparation_deadline: datetime) -> str:
        if not self.snapshot.login.email.strip():
            raise _StopTask(TaskPhase.FAILED, "未配置登录账号，不能开始自动登录")
        navigation = self.session.navigate_to_cart()
        if navigation.uncertain:
            raise _StopTask(TaskPhase.STOPPED, "打开购物车以检查登录状态的结果不明，未继续自动流程")
        if not navigation.ok:
            raise _StopTask(TaskPhase.UNSUPPORTED, navigation.error or "无法打开官方购物车")
        auto_attempted = False
        manual_reason: str | None = None
        while True:
            self._check_stop()
            if self.clock.now() >= preparation_deadline:
                raise _StopTask(TaskPhase.MISSED, "登录准备达到时间上限，未开始购买窗口")
            state = self.session.read_login_state()
            if state.status is LoginStatus.LOGGED_IN:
                if self._identity_matches(state.account_identity):
                    self._emit(
                        TaskPhase.PREPARING,
                        "已核实配置账号的登录状态",
                        {"login_mode": "REUSED"},
                    )
                    return "REUSED"
                manual_reason = "当前登录账号与配置不一致，或无法核实账号身份"
                self._emit_once(manual_reason, TaskPhase.AWAITING_MANUAL_LOGIN, manual_reason + "；请在专用 Chrome 中切换或确认账号")
                if self._wait_manual_login(preparation_deadline):
                    return "MANUAL"
            if state.status in (LoginStatus.BLOCKED, LoginStatus.SESSION_LOST):
                raise _StopTask(TaskPhase.UNSUPPORTED, "登录页面访问受限或会话失效，不能自动登录")
            if state.status is LoginStatus.CHALLENGE:
                manual_reason = state.error or "官网要求验证码、人机或额外认证"
                self._emit_once(manual_reason, TaskPhase.AWAITING_MANUAL_LOGIN, manual_reason + "；请在专用 Chrome 中人工完成")
                if self._wait_manual_login(preparation_deadline):
                    return "MANUAL"
            if state.status in (LoginStatus.ERROR, LoginStatus.UNRECOGNIZED):
                self._emit_once("登录状态暂不可核实", TaskPhase.PREPARING, "登录状态暂不可核实，有限等待页面可确认")
                self._sleep()
                continue
            if auto_attempted:
                manual_reason = state.error or manual_reason or "自动登录未成功"
                self._emit_once(manual_reason, TaskPhase.AWAITING_MANUAL_LOGIN, manual_reason + "；请在专用 Chrome 中人工完成")
                if self._wait_manual_login(preparation_deadline):
                    return "MANUAL"
            auto_attempted = True
            self._emit(
                TaskPhase.AUTO_LOGIN,
                "未发现配置账号的有效会话，使用本地资料尝试一轮官方表单自动登录",
                {"login_mode": "AUTO"},
            )
            result = self.session.login_with_credentials(self.snapshot.login)
            if result.attempted:
                state = self._poll_auto_login_result(preparation_deadline)
                if state.status is LoginStatus.LOGGED_IN and self._identity_matches(state.account_identity):
                    self._emit(
                        TaskPhase.PREPARING,
                        "自动登录成功并已核实配置账号",
                        {"login_mode": "AUTO"},
                    )
                    return "AUTO"
            manual_reason = result.error or (state.error if state is not None else None) or "自动登录未成功"
            self._emit(
                TaskPhase.AWAITING_MANUAL_LOGIN,
                manual_reason + "；请在同一专用 Chrome 中人工完成登录，程序不会重复提交凭据或发码",
                {"login_mode": "MANUAL"},
            )
            if self._wait_manual_login(preparation_deadline):
                return "MANUAL"

    def _poll_auto_login_result(self, preparation_deadline: datetime) -> LoginRead:
        deadline = min(
            preparation_deadline,
            self.clock.now() + timedelta(seconds=self.login_result_timeout_seconds),
        )
        state = LoginRead(status=LoginStatus.NOT_LOGGED_IN, error="自动登录结果未返回")
        while self.clock.now() < deadline:
            self._check_stop()
            state = self.session.read_login_state()
            if state.status is LoginStatus.LOGGED_IN and self._identity_matches(state.account_identity):
                return state
            if state.status in (LoginStatus.CHALLENGE, LoginStatus.BLOCKED, LoginStatus.SESSION_LOST):
                return state
            if state.status is LoginStatus.NOT_LOGGED_IN and state.error:
                return state
            self._sleep()
        return state

    def _wait_manual_login(self, preparation_deadline: datetime) -> bool:
        deadline = min(
            preparation_deadline,
            self.clock.now() + timedelta(seconds=self.manual_login_timeout_seconds),
        )
        last_reason: str | None = None
        while self.clock.now() < deadline:
            self._check_stop()
            state = self.session.read_login_state()
            if state.status is LoginStatus.LOGGED_IN and self._identity_matches(state.account_identity):
                self._emit(
                    TaskPhase.PREPARING,
                    "人工登录已完成并核实配置账号",
                    {"login_mode": "MANUAL"},
                )
                return True
            if state.status in (LoginStatus.BLOCKED, LoginStatus.SESSION_LOST):
                raise _StopTask(TaskPhase.UNSUPPORTED, "人工登录页面访问受限或会话失效")
            if state.status is LoginStatus.LOGGED_IN:
                reason = "当前登录账号仍与配置不一致或无法核实，请确认账号"
            elif state.status is LoginStatus.CHALLENGE:
                reason = "官网仍要求验证码、人机或额外认证，请人工完成"
            elif state.status is LoginStatus.NOT_LOGGED_IN:
                reason = "仍在等待人工登录完成，未核实成功前不会加购或付款"
            else:
                reason = "登录状态暂不可核实，继续保持准备中"
            self._emit_once(last_reason, TaskPhase.AWAITING_MANUAL_LOGIN, reason)
            last_reason = reason
            self._sleep()
        raise _StopTask(TaskPhase.MISSED, "人工登录等待超过 10 分钟上限，购买窗口未开启")

    def _emit_once(self, last_reason: str | None, phase: TaskPhase, message: str) -> None:
        if last_reason != message:
            self._emit(phase, message)

    def _target(self):
        return self.snapshot.target.preset

    def _sleep_interruptible(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            self._check_stop()
            step = min(0.1, remaining)
            self.clock.sleep(step)
            remaining -= step

    def _wait_for_available_product(self, preset) -> ProductPageState:
        self._emit(TaskPhase.OPENING_PRODUCT, "打开已确认的目标商品页")
        product = self.session.open_product(preset.url)
        if product.status is ReadStatus.BLOCKED:
            raise _StopTask(TaskPhase.UNSUPPORTED, product.error or "商品页访问受限")
        last_reason: str | None = None
        read_errors = 0
        while True:
            self._check_stop()
            product = self.session.read_product_page()
            if product.status is ReadStatus.BLOCKED:
                raise _StopTask(TaskPhase.UNSUPPORTED, product.error or "商品页访问受限")
            if product.status in (ReadStatus.ERROR, ReadStatus.UNRECOGNIZED, ReadStatus.NOT_AVAILABLE):
                read_errors += 1
                if read_errors >= READ_ERROR_LIMIT:
                    raise _StopTask(TaskPhase.STOPPED, "商品页连续读取异常 3 次，停止等待")
                self._emit_once(
                    last_reason,
                    TaskPhase.WAITING_AVAILABILITY,
                    f"商品页暂时无法读取（{read_errors}/{READ_ERROR_LIMIT}），稍后重试",
                )
                last_reason = "等待商品页"
                self._maybe_safe_product_refresh()
                self._sleep_interruptible(TRANSIENT_RETRY_INTERVAL_SECONDS)
                continue
            read_errors = 0
            if not product.title or normalize_text(product.title) != normalize_text(preset.title):
                raise _StopTask(
                    TaskPhase.STOPPED,
                    f"商品身份不一致：页面为 {product.title or '未知'}，预设为 {preset.title}",
                )
            if not product.single_variant and product.variants:
                if normalize_text(product.selected_variant or "") != normalize_text(preset.variant):
                    selection = self.session.select_variant(preset.variant)
                    if not selection.ok:
                        raise _StopTask(TaskPhase.STOPPED, "规格设置失败：" + (selection.error or "无法选择预设规格"))
                    continue
            if product.quantity != preset.quantity:
                quantity_result = self.session.set_quantity(preset.quantity)
                if not quantity_result.ok:
                    raise _StopTask(TaskPhase.STOPPED, "数量设置失败：" + (quantity_result.error or "无法设置完整数量"))
                continue
            product_reason = self._validate_product_price(product)
            if product_reason is not None:
                if product_reason.startswith("暂不可购"):
                    self._emit_once(last_reason, TaskPhase.WAITING_AVAILABILITY, product_reason)
                    last_reason = product_reason
                    self._maybe_safe_product_refresh()
                    self._sleep_interruptible(SOLD_OUT_INTERVAL_SECONDS)
                    continue
                raise _StopTask(TaskPhase.STOPPED, product_reason)
            return product

    def _execute_full_flow(self, window: ActiveWindow) -> TaskOutcome:
        preset = self._target()
        self._assert_session_account()
        product = self._wait_for_available_product(preset)
        empty_checked = False
        attempt_seq = 1
        while True:
            self._check_stop()
            self._assert_session_account()
            if not empty_checked:
                empty_result = self._verify_empty_cart_again()
                if empty_result is not None:
                    raise _StopTask(TaskPhase.STOPPED, empty_result)
                empty_checked = True
                product = self._wait_for_available_product(preset)
                continue
            try:
                successful = self._attempt_add(product, attempt_seq)
            except _RetryAdd as retry:
                self._emit_once(None, TaskPhase.WAITING_AVAILABILITY, retry.message)
                self._sleep_interruptible(retry.interval)
                empty_checked = False
                continue
            if successful:
                self._emit(TaskPhase.CHECKING_OUT, "加购完整成功，重新核对后立即进入官方结算")
                return self._checkout_and_submit()
            attempt_seq += 1
            self._emit(
                TaskPhase.WAITING_AVAILABILITY,
                "本次加购未发出或官网明确暂拒且无副作用；等待后重新核对同一目标",
                {"attempt": attempt_seq - 1},
            )
            self._sleep_interruptible(TRANSIENT_RETRY_INTERVAL_SECONDS)
            empty_checked = False

    def _validate_product_price(self, product: ProductPageState) -> str | None:
        preset = self._target()
        if product.currency is None:
            return "当前商品币种无法确认"
        if product.currency.upper() != "USD":
            return f"当前币种不是 USD：{product.currency}"
        if product.unit_price_usd is None:
            return "当前 USD 单价无法确认"
        if product.unit_price_usd > preset.unit_price_limit_usd:
            return (
                f"当前单价 {format_usd(product.unit_price_usd)} 超过上限 "
                f"{format_usd(preset.unit_price_limit_usd)}"
            )
        if not product.add_to_cart_enabled:
            return "暂不可购：加购按钮当前不可用"
        return None

    def _verify_empty_cart_again(self) -> str | None:
        navigation = self.session.navigate_to_cart()
        if navigation.uncertain:
            return "实际加购前打开购物车的结果不明"
        if not navigation.ok:
            return navigation.error or "实际加购前无法打开购物车"
        cart = self.session.read_cart()
        if cart.status is ReadStatus.EMPTY or (cart.status is ReadStatus.OK and not cart.lines):
            return None
        if cart.status is ReadStatus.OK and cart.lines:
            return "实际加购前购物车已有商品，停止并请人工清理"
        if cart.status in (ReadStatus.NOT_LOGGED_IN,):
            return "实际加购前登录状态已失效"
        if cart.status is ReadStatus.BLOCKED:
            return "实际加购前购物车访问受限"
        return "实际加购前无法确认空购物车"

    def _maybe_safe_product_refresh(self) -> None:
        if not self.allow_product_refresh:
            return
        now = time.monotonic()
        if self._last_refresh_monotonic is not None and now - self._last_refresh_monotonic < 1.0:
            return
        self._last_refresh_monotonic = now
        try:
            self.session.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _attempt_add(self, product: ProductPageState, attempt_seq: int) -> bool:
        preset = self._target()
        fresh = self.session.read_product_page()
        if fresh.status is not ReadStatus.OK:
            raise _StopTask(TaskPhase.STOPPED, fresh.error or "加购前无法重新读取商品页")
        if not fresh.title or normalize_text(fresh.title) != normalize_text(preset.title):
            raise _StopTask(TaskPhase.STOPPED, "加购前商品身份发生变化，未点击加购")
        if not fresh.single_variant and fresh.variants:
            if normalize_text(fresh.selected_variant or "") != normalize_text(preset.variant):
                raise _StopTask(TaskPhase.STOPPED, "加购前规格发生变化，未点击加购")
        if fresh.quantity != preset.quantity:
            raise _StopTask(TaskPhase.STOPPED, "加购前数量发生变化，未点击加购")
        price_reason = self._validate_product_price(fresh)
        if price_reason is not None:
            if price_reason.startswith("暂不可购"):
                raise _RetryAdd(price_reason, interval=SOLD_OUT_INTERVAL_SECONDS)
            raise _StopTask(TaskPhase.STOPPED, "加购前复核失败：" + price_reason)
        if fresh.unit_price_usd is None:
            raise _StopTask(TaskPhase.STOPPED, "加购前 USD 单价无法确认")
        product = fresh
        self._assert_session_account()
        now = self.clock.now()
        record = build_add_to_cart_record(
            task_id=self.snapshot.task_id,
            target=preset,
            now=now,
            attempt_seq=attempt_seq,
        )
        try:
            self.store.begin_add_to_cart(record, now=now)
        except Exception as exc:  # noqa: BLE001 - never click without a durable record
            raise _StopTask(TaskPhase.FAILED, f"无法保存加购在途记录，未触发加购：{exc}") from exc
        self._emit(
            TaskPhase.ADDING_TO_CART,
            f"空车与商品核对通过，第 {attempt_seq} 次加购记录已保存，准备触发一次加购",
            {
                "title": preset.title,
                "variant": preset.variant,
                "quantity": preset.quantity,
                "unit_price_usd": format_usd(product.unit_price_usd or preset.unit_price_limit_usd),
                "attempt": attempt_seq,
            },
        )
        if self.cancel_event.is_set():
            try:
                self._update_add(record, AddToCartState.NOT_SENT, "记录保存后、点击前收到停止请求")
            except Exception:  # noqa: BLE001 - retain blocking record if conclusion cannot be saved
                pass
            raise _StopTask(TaskPhase.STOPPED, "收到停止请求，未触发加购")
        try:
            result = self.session.add_to_cart()
        except Exception as exc:  # noqa: BLE001 - unknown whether click reached site
            record = self._update_add(record, AddToCartState.UNKNOWN, f"加购动作异常：{exc}")
            raise _StopTask(TaskPhase.UNKNOWN, "加购结果不明；已保留未决记录，禁止重复加购") from exc
        if not result.attempted and not result.ok:
            self._update_add(record, AddToCartState.NOT_SENT, result.error or "加购在点击前不可用")
            return False
        record = self._update_add(record, AddToCartState.IN_FLIGHT, "加购已触发，等待购物车核对")
        self._emit(TaskPhase.VERIFYING_CART, "加购已触发；仅核对一次结果，不补点或重发")
        confirmed, reason, _empty = self._verify_add_result()
        if confirmed:
            self._update_add(record, AddToCartState.CONFIRMED, "购物车精确匹配")
            return True
        if getattr(result, "confirmed_no_side_effect", False) and _empty:
            self._update_add(record, AddToCartState.NOT_SENT, reason or "官网明确暂拒且购物车为空")
            return False
        self._update_add(record, AddToCartState.UNKNOWN, reason or "加购结果无法确认")
        raise _StopTask(TaskPhase.UNKNOWN, reason or "加购结果不明；保留未决记录并阻止新任务")

    def _update_add(
        self,
        record: AddToCartRecord,
        state: AddToCartState,
        note: str,
    ) -> AddToCartRecord:
        from dataclasses import replace

        updated = replace(
            record,
            state=state,
            note=note,
            updated_at_utc=self.clock.now().astimezone(UTC).isoformat(timespec="seconds"),
        )
        return self.store.update_add_to_cart(updated, now=self.clock.now())

    def _verify_add_result(self) -> tuple[bool, str | None, bool]:
        preset = self._target()
        verify_deadline = self.clock.now() + timedelta(seconds=STEP_TIMEOUT_SECONDS)
        last_reason = "等待购物车显示本次加购结果"
        saw_empty = False
        failures = 0
        while self.clock.now() < verify_deadline:
            self._check_stop()
            navigation = self.session.navigate_to_cart()
            if navigation.uncertain:
                return False, "加购后打开购物车的结果不明", saw_empty
            if not navigation.ok:
                failures += 1
                if failures >= READ_ERROR_LIMIT:
                    return False, navigation.error or "加购后连续无法打开购物车", saw_empty
                last_reason = navigation.error or "加购后无法打开购物车"
                self._sleep()
                continue
            cart = self.session.read_cart()
            if cart.status in (ReadStatus.SESSION_LOST, ReadStatus.BLOCKED):
                return False, "加购后会话或访问失效", saw_empty
            if cart.status is ReadStatus.OK:
                content = match_single_target(cart.lines, preset, currency=cart.currency)
                if content.ok:
                    return True, None, saw_empty
                last_reason = content.reason or "购物车内容不符合唯一目标"
                return False, last_reason, saw_empty
            if cart.status is ReadStatus.EMPTY:
                saw_empty = True
                last_reason = "购物车仍为空，等待加购结果"
            else:
                failures += 1
                if failures >= READ_ERROR_LIMIT:
                    return False, "加购后购物车连续读取异常", saw_empty
                last_reason = "加购后购物车暂时无法读取"
            self._sleep()
        return False, last_reason, saw_empty

    def _checkout_and_submit(self) -> TaskOutcome:
        preset = self._target()
        entered_checkout = False
        shipping: ShippingSnapshot | None = None
        last_reason: str | None = None
        entry_started = self.clock.now()
        read_failures = 0
        while True:
            self._check_stop()
            self._assert_session_account()
            if not entered_checkout and (self.clock.now() - entry_started).total_seconds() >= STEP_TIMEOUT_SECONDS:
                raise _StopTask(TaskPhase.STOPPED, "结算入口等待超过 30 秒，停止并保留浏览器")
            if not entered_checkout:
                cart = self.session.read_cart()
                if cart.status in (ReadStatus.SESSION_LOST, ReadStatus.NOT_LOGGED_IN):
                    raise _StopTask(TaskPhase.STOPPED, "购物车会话失效，停止并保留浏览器")
                if cart.status is ReadStatus.BLOCKED:
                    raise _StopTask(TaskPhase.UNSUPPORTED, "购物车访问受限，停止")
                content = match_single_target(cart.lines, preset, currency=cart.currency)
                if not content.ok:
                    raise _StopTask(TaskPhase.STOPPED, "购物车内容与唯一目标不一致：" + (content.reason or ""))
                entered = self.session.enter_checkout()
                if entered.uncertain:
                    raise _StopTask(TaskPhase.STOPPED, "去结算动作结果不明，未重复导航")
                if entered.ok:
                    entered_checkout = True
                    self._emit(TaskPhase.CHECKING_OUT, "已进入官方结算，继续填写与核对")
                    continue
                self._emit_once(last_reason, TaskPhase.CHECKING_OUT, "官方结算入口暂未开放")
                last_reason = "等待结算入口"
                self._sleep()
                continue
            checkout = self.session.read_checkout()
            if checkout.status in (ReadStatus.SESSION_LOST, ReadStatus.NOT_LOGGED_IN):
                raise _StopTask(TaskPhase.STOPPED, "结算会话失效，停止并保留浏览器")
            if checkout.status is ReadStatus.BLOCKED:
                raise _StopTask(TaskPhase.UNSUPPORTED, "结算页面访问受限，停止")
            if checkout.status in (ReadStatus.ERROR, ReadStatus.UNRECOGNIZED, ReadStatus.NOT_AVAILABLE):
                read_failures += 1
                if read_failures >= READ_ERROR_LIMIT:
                    raise _StopTask(TaskPhase.STOPPED, "结算页连续读取异常 3 次，停止并保留浏览器")
                self._emit_once(last_reason, TaskPhase.REVALIDATING, "结算页暂时无法读取，有限只读重试")
                last_reason = "等待结算页"
                self._sleep()
                continue
            read_failures = 0
            content = match_single_target(checkout.lines, preset, currency=checkout.currency)
            if not content.ok:
                raise _StopTask(TaskPhase.STOPPED, "结算订单摘要与唯一目标不一致：" + (content.reason or ""))
            if self._form_reason(checkout) is not None:
                result = self.session.fill_checkout(self.snapshot.customer, self.snapshot.card, self.policy)
                if not result.ok:
                    raise _StopTask(TaskPhase.FAILED, "结算资料填写失败：" + (result.error or "未知错误"))
                checkout = self.session.read_checkout()
            readiness = evaluate_checkout(checkout, preset, self.snapshot.target.order_limit_usd)
            if readiness.level is not PreparedLevel.CHECKOUT_READY:
                if self._is_transient_checkout_gap(checkout):
                    self._emit_once(last_reason, TaskPhase.REVALIDATING, "等待官方报价或结算页完成")
                    last_reason = "等待报价"
                    self._sleep()
                    continue
                raise _StopTask(TaskPhase.STOPPED, "结算内容不符合预期：" + ("；".join(readiness.reasons) or "无法确认"))
            if checkout.payment_origin is not None:
                source = ensure_trusted_payment_origin(self.policy, checkout.payment_origin)
                if not source.ok:
                    raise _StopTask(TaskPhase.FAILED, "支付页面来源未核实，停止付款")
            if shipping is None:
                shipping, shipping_reason = freeze_shipping(checkout)
                if shipping is None:
                    raise _StopTask(TaskPhase.FAILED, shipping_reason or "默认配送无法确认")
                self._emit(
                    TaskPhase.REVALIDATING,
                    "已固定本次默认配送快照；下一次读取将再次核对服务与 USD 运费后才付款",
                    shipping.to_dict(),
                )
                self._sleep(0)
                continue
            shipping_reason = verify_shipping_snapshot(checkout, shipping)
            if shipping_reason:
                raise _StopTask(TaskPhase.STOPPED, shipping_reason)
            if not checkout.final_submit_enabled:
                self._emit_once(last_reason, TaskPhase.REVALIDATING, "最终购买按钮尚未开放，窗口内继续观察")
                last_reason = "等待最终提交"
                self._sleep()
                continue
            final = verify_before_submit(
                checkout,
                preset,
                self.snapshot.target.order_limit_usd,
                shipping,
            )
            if final.level is not PreparedLevel.CHECKOUT_READY:
                raise _StopTask(TaskPhase.STOPPED, "付款前复核未通过：" + ("；".join(final.reasons) or "无法确认"))
            return self._submit_purchase(checkout, shipping)

    def _form_reason(self, checkout: CheckoutRead) -> str | None:
        if checkout.form is None:
            return None
        if checkout.form.errors or not checkout.form.contact_complete or not checkout.form.card_complete:
            return "表单待填写"
        return None

    def _is_transient_checkout_gap(self, checkout: CheckoutRead) -> bool:
        if checkout.form is None:
            return True
        if checkout.subtotal_usd is None or checkout.tax_usd is None or checkout.shipping_total_usd is None:
            return True
        if checkout.total_usd is None:
            return True
        return False

    def _submit_purchase(
        self,
        checkout: CheckoutRead,
        shipping: ShippingSnapshot,
    ) -> TaskOutcome:
        preset = self._target()
        now = self.clock.now()
        self._check_stop()
        self._assert_session_account()
        assert checkout.total_usd is not None
        record = build_submission_record(
            task_id=self.snapshot.task_id,
            preset=preset,
            order_limit_usd=self.snapshot.target.order_limit_usd,
            prepared_level=PreparedLevel.CHECKOUT_READY,
            planned_start_utc=None,
            deadline_utc=now + timedelta(seconds=self.result_timeout_seconds),
            shipping=shipping,
            total_usd=checkout.total_usd,
            now=now,
        )
        try:
            self.store.begin_purchase(record, now=now)
        except Exception as exc:  # noqa: BLE001
            raise _StopTask(TaskPhase.FAILED, f"无法保存购买提交状态，未触发付款：{exc}") from exc
        self._emit(TaskPhase.SUBMITTING, "完整订单核对通过，已记录提交状态，准备一次官方购买提交")
        if self.cancel_event.is_set():
            self._mark_purchase_not_submitted(record, "提交记录保存后、点击前收到停止请求")
            raise _StopTask(TaskPhase.STOPPED, "收到停止请求，未触发付款")
        try:
            result = self.session.submit_purchase()
        except Exception as exc:  # noqa: BLE001
            record = self._update_purchase(record, state=SubmissionState.UNKNOWN, note=f"提交动作异常：{exc}")
            self._emit(TaskPhase.AWAITING_VERIFICATION, "付款提交结果不明，禁止重复提交；请人工核对")
            return self._await_result(record)
        if not result.attempted and result.sent is False:
            self._mark_purchase_not_submitted(record, result.error or "最终按钮在点击前不可用")
            raise _StopTask(TaskPhase.NOT_SUBMITTED, result.error or "最终购买未被发出")
        if result.error:
            record = self._update_purchase(record, state=SubmissionState.UNKNOWN, note=result.error)
        self._emit(TaskPhase.AWAITING_VERIFICATION, "购买提交已发出；仅核对结果，不再重复提交")
        return self._await_result(record)

    def _mark_purchase_not_submitted(self, record: SubmissionRecord, note: str) -> None:
        updated = self._update_purchase(record, state=SubmissionState.NOT_SUBMITTED, note=note)
        self._emit(TaskPhase.NOT_SUBMITTED, note, {"state": str(updated.state)})

    def _update_purchase(self, record: SubmissionRecord, **changes) -> SubmissionRecord:
        from dataclasses import replace

        updated = replace(
            record,
            updated_at_utc=self.clock.now().astimezone(UTC).isoformat(timespec="seconds"),
            **changes,
        )
        snapshot = self.store.load()
        self.store._write(
            add_to_cart=snapshot.add_to_cart,
            purchase=updated,
            flow_type="full_baseline",
            now=self.clock.now(),
            add_to_cart_attempts=snapshot.add_to_cart_attempts,
        )
        return updated

    def _await_result(self, record: SubmissionRecord) -> TaskOutcome:
        deadline = self.clock.now() + timedelta(seconds=self.result_timeout_seconds)
        while self.clock.now() < deadline:
            if self.cancel_event.is_set():
                try:
                    record = self._update_purchase(
                        record,
                        state=SubmissionState.UNKNOWN,
                        note="用户停止；自动化已停止，结果仍需在官方渠道核对",
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise _StopTask(
                    TaskPhase.STOPPED,
                    "已停止自动操作；购买提交可能已发出，请人工核对官方订单，禁止直接重试",
                )
            try:
                result = self.session.read_order_result()
            except Exception as exc:  # noqa: BLE001
                result = OrderResult(status=OrderStatus.UNKNOWN, evidence=str(exc))
            state, note = classify_order_result(result, self._target())
            if state is SubmissionState.SUCCEEDED:
                record = self._update_purchase(
                    record,
                    state=state,
                    outcome=state.value,
                    order_id=result.order_id,
                    payment_state=result.payment_state,
                    note=note,
                )
                self._emit(TaskPhase.SUCCEEDED, note, {"order_id": result.order_id})
                return TaskOutcome(
                    phase=TaskPhase.SUCCEEDED,
                    message=note,
                    prepared_level=PreparedLevel.CHECKOUT_READY,
                    record=record,
                    events=list(self.events_log),
                    performance=list(self.performance),
                )
            if state is SubmissionState.DECLINED:
                record = self._update_purchase(
                    record,
                    state=state,
                    outcome=state.value,
                    order_id=result.order_id,
                    payment_state=result.payment_state,
                    note=note,
                )
                self._emit(TaskPhase.FAILED, note)
                return TaskOutcome(
                    phase=TaskPhase.FAILED,
                    message=note,
                    prepared_level=PreparedLevel.CHECKOUT_READY,
                    record=record,
                    events=list(self.events_log),
                    performance=list(self.performance),
                )
            self._sleep(self.result_poll_interval_seconds)
        record = self._update_purchase(
            record,
            state=SubmissionState.UNKNOWN,
            note="结果等待期限结束仍未确认；请人工核对官方订单",
        )
        self._emit(TaskPhase.UNKNOWN, "结果等待期限结束仍未确认；请人工核对官方订单")
        return TaskOutcome(
            phase=TaskPhase.UNKNOWN,
            message="购买提交结果不明；禁止自动重复提交",
            prepared_level=PreparedLevel.CHECKOUT_READY,
            record=record,
            events=list(self.events_log),
            performance=list(self.performance),
        )


def performance_summary(
    samples: list[PerformanceSample],
    path: PreparedLevel | None = None,
    action: str | None = None,
) -> dict[str, float | int | None]:
    values = [
        sample.client_ms
        for sample in samples
        if (path is None or sample.path is path) and (action is None or sample.action == action)
    ]
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "count": len(ordered),
        "p50_ms": round(float(median(ordered)), 3),
        "p95_ms": round(float(ordered[index]), 3),
        "max_ms": round(float(ordered[-1]), 3),
    }


@dataclass(frozen=True)
class StartResult:
    ok: bool
    message: str
    task_id: str | None = None


class TaskManager:
    def __init__(
        self,
        *,
        store: SubmissionStore,
        clock: Clock | None = None,
        events: "queue.Queue[TaskEvent] | None" = None,
        policy: TrustedSourcePolicy | None = None,
        runner_factory=PurchaseRunner,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.events = events or queue.Queue()
        self.policy = policy or TrustedSourcePolicy()
        self.runner_factory = runner_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._task_id: str | None = None
        self._outcome: TaskOutcome | None = None
        self._session: PurchaseSession | None = None

    @property
    def events_queue(self) -> "queue.Queue[TaskEvent]":
        return self.events

    @property
    def task_id(self) -> str | None:
        return self._task_id

    @property
    def outcome(self) -> TaskOutcome | None:
        return self._outcome

    def is_active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, snapshot: TaskSnapshot, session: PurchaseSession) -> StartResult:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return StartResult(False, "已有任务正在准备、执行或等待结果，不能同时启动第二个任务")
            cancel = threading.Event()
            task_id = snapshot.task_id
            runner = self.runner_factory(
                snapshot=snapshot,
                session=session,
                store=self.store,
                clock=self.clock,
                events=self.events,
                cancel_event=cancel,
                policy=self.policy,
            )
            self._cancel = cancel
            self._task_id = task_id
            self._session = session
            self._outcome = None

            def _target() -> None:
                try:
                    self._outcome = runner.run()
                finally:
                    pass

            self._thread = threading.Thread(target=_target, name=f"topps-task-{task_id}", daemon=True)
            self._thread.start()
            return StartResult(True, "任务已启动", task_id=task_id)

    def stop(self) -> None:
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
                self.events.put(
                    TaskEvent(
                        at_utc=self.clock.now(),
                        phase=TaskPhase.STOPPING,
                        message="已收到停止请求，等待在途操作按有限超时退出",
                    )
                )

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._handle = None
        self._key: str | None = None

    def acquire(self) -> bool:
        import tempfile

        self._path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self._path.resolve())
        if key in _HELD_LOCKS:
            return False
        handle = open(self._path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        _HELD_LOCKS.add(key)
        self._key = key
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        finally:
            if self._key is not None:
                _HELD_LOCKS.discard(self._key)
            self._handle = None
            self._key = None

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            raise RuntimeError("同一用户数据目录已有程序实例在运行")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


_HELD_LOCKS: set[str] = set()


def new_task_id() -> str:
    return uuid.uuid4().hex
