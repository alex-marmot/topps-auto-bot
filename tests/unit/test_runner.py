from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support import (
    FakeClock,
    FakeSession,
    make_cart,
    make_checkout,
    make_config,
    make_line,
    make_snapshot,
    shipping_snapshot,
)
from topps_auto_bot.browser import TrustedSourcePolicy
from topps_auto_bot.models import (
    ActionResult,
    FormState,
    OrderResult,
    OrderStatus,
    PageKind,
    PreparedLevel,
    ReadStatus,
    ScheduleMode,
    ScheduleSettings,
    ShippingOption,
    SubmissionState,
    TaskPhase,
    UTC,
    parse_beijing_datetime,
)
from topps_auto_bot.purchase import SubmissionStore
from topps_auto_bot.runner import (
    PurchaseRunner,
    SingleInstanceLock,
    TaskManager,
    describe_window,
    performance_summary,
)


def run_runner(
    *,
    session: FakeSession,
    clock: FakeClock,
    tmp_path: Path,
    snapshot=None,
    cancel: threading.Event | None = None,
    policy: TrustedSourcePolicy | None = None,
    preparation_timeout: float = 60.0,
):
    snapshot = snapshot or make_snapshot(config=make_config())
    events: "queue.Queue" = queue.Queue()
    store = SubmissionStore(tmp_path / "run-state.json")
    runner = PurchaseRunner(
        snapshot=snapshot,
        session=session,
        store=store,
        clock=clock,
        events=events,
        cancel_event=cancel or threading.Event(),
        policy=policy or TrustedSourcePolicy(allow_loopback_http=True),
        preparation_timeout_seconds=preparation_timeout,
        result_timeout_seconds=60.0,
        poll_interval_seconds=0.1,
        result_poll_interval_seconds=1.0,
    )
    outcome = runner.run()
    return runner, outcome, store, events


def test_immediate_cart_path_submits_once_and_confirms(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CART)
    clock = FakeClock()
    runner, outcome, store, _events = run_runner(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.enter_calls == 1
    assert session.submit_calls == 1
    assert store.load().record is not None
    assert store.load().record.state is SubmissionState.SUCCEEDED
    assert runner.performance
    assert any(sample.action == "enter_checkout" for sample in runner.performance)
    assert any(sample.action == "submit_purchase" for sample in runner.performance)


def test_immediate_checkout_path_skips_cart(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CHECKOUT, checkout=make_checkout())
    runner, outcome, _store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.enter_calls == 0
    assert session.submit_calls == 1


def test_window_expiry_without_submit(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CHECKOUT, checkout=make_checkout(final_submit_enabled=False))
    runner, outcome, store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.EXPIRED
    assert session.submit_calls == 0
    assert store.load().status.name == "MISSING"


def test_cart_navigation_after_deadline_does_not_submit(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CART)
    clock = FakeClock()
    def on_enter() -> None:
        pass
    original_enter = session.enter_checkout
    def enter_and_pass_deadline():
        result = original_enter()
        clock.advance(61)
        return result
    session.enter_checkout = enter_and_pass_deadline  # type: ignore[assignment]
    _runner, outcome, _store, _events = run_runner(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.EXPIRED
    assert session.submit_calls == 0


def test_scheduled_starts_at_planned_time_not_wake_time(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC))
    planned = datetime(2026, 11, 6, 6, 0, 10, tzinfo=UTC)
    snapshot = make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=planned, window_seconds=60),
    )
    session = FakeSession(page=PageKind.CHECKOUT)
    results: list = []
    original_submit = session.submit_purchase
    def submit_and_record():
        results.append(clock.now())
        return original_submit()
    session.submit_purchase = submit_and_record  # type: ignore[assignment]
    _runner, outcome, _store, _events = run_runner(session=session, clock=clock, tmp_path=tmp_path, snapshot=snapshot)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert results[0] == planned


def test_scheduled_not_ready_at_start_is_missed(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC))
    planned = datetime(2026, 11, 6, 6, 0, 5, tzinfo=UTC)
    snapshot = make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=planned, window_seconds=60),
    )
    session = FakeSession(page=PageKind.OTHER)
    _runner, outcome, _store, _events = run_runner(session=session, clock=clock, tmp_path=tmp_path, snapshot=snapshot)
    assert outcome.phase is TaskPhase.MISSED
    assert session.submit_calls == 0


class JumpClock(FakeClock):
    def __init__(self, start, jump_at, jump_to) -> None:
        super().__init__(start)
        self.jump_at = jump_at
        self.jump_to = jump_to

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        if self.current >= self.jump_at:
            self.current = self.jump_to


def test_scheduled_delayed_wake_uses_original_deadline(tmp_path: Path) -> None:
    planned = datetime(2026, 11, 6, 6, 1, 0, tzinfo=UTC)
    clock = JumpClock(datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC), planned, planned + timedelta(seconds=5))
    snapshot = make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=planned, window_seconds=60),
    )
    session = FakeSession(page=PageKind.CHECKOUT)
    _runner, outcome, _store, _events = run_runner(
        session=session,
        clock=clock,
        tmp_path=tmp_path,
        snapshot=snapshot,
    )
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert outcome.window is not None
    assert outcome.window.effective_start_utc == planned
    assert outcome.window.deadline_utc == planned + timedelta(seconds=60)


def test_scheduled_wake_after_deadline_does_not_submit(tmp_path: Path) -> None:
    planned = datetime(2026, 11, 6, 6, 1, 0, tzinfo=UTC)
    clock = JumpClock(datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC), planned, planned + timedelta(seconds=61))
    snapshot = make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=planned, window_seconds=60),
    )
    session = FakeSession(page=PageKind.CHECKOUT)
    _runner, outcome, store, _events = run_runner(
        session=session,
        clock=clock,
        tmp_path=tmp_path,
        snapshot=snapshot,
    )
    assert outcome.phase in (TaskPhase.EXPIRED, TaskPhase.MISSED)
    assert session.submit_calls == 0
    assert store.load().status.name == "MISSING"


def test_scheduled_window_already_expired_is_missed(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 6, 6, 5, 0, tzinfo=UTC))
    planned = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    snapshot = make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=planned, window_seconds=60),
    )
    session = FakeSession(page=PageKind.CHECKOUT)
    _runner, outcome, _store, _events = run_runner(session=session, clock=clock, tmp_path=tmp_path, snapshot=snapshot)
    assert outcome.phase is TaskPhase.MISSED
    assert session.submit_calls == 0


def test_external_config_change_does_not_affect_active_snapshot(tmp_path: Path) -> None:
    from topps_auto_bot.settings import load_config, save_config

    path = tmp_path / "config.json"
    config = make_config()
    save_config(path, config)
    snapshot = make_snapshot(config=config)
    changed = make_config(order_limit="1.00")
    save_config(path, changed)
    assert load_config(path).config is not None
    session = FakeSession(page=PageKind.CHECKOUT)
    _runner, outcome, _store, _events = run_runner(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        snapshot=snapshot,
    )
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert snapshot.target.order_limit_usd == Decimal("70.00")


def test_pre_existing_pending_submission_blocks_task(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / "run-state.json")
    session = FakeSession(page=PageKind.CHECKOUT)
    snapshot = make_snapshot(config=make_config())
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    from topps_auto_bot.purchase import build_submission_record

    store.begin(
        build_submission_record(
            task_id="old-task",
            preset=snapshot.target.preset,
            order_limit_usd=Decimal("70.00"),
            prepared_level=PreparedLevel.CHECKOUT_READY,
            planned_start_utc=None,
            deadline_utc=now,
            shipping=shipping_snapshot(),
            total_usd=Decimal("55.00"),
            now=now,
        )
    )
    runner = PurchaseRunner(
        snapshot=snapshot,
        session=session,
        store=store,
        clock=FakeClock(),
        events=queue.Queue(),
        cancel_event=threading.Event(),
        policy=TrustedSourcePolicy(allow_loopback_http=True),
    )
    outcome = runner.run()
    assert outcome.phase is TaskPhase.BLOCKED
    assert session.submit_calls == 0


def test_shipping_change_after_checkout_ready_stops(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CHECKOUT)
    calls = {"n": 0}
    def on_read(s: FakeSession) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            s.checkout = make_checkout(
                shipping_options=(ShippingOption("express", "Express", Decimal("10.00"), True),)
            )
    session.on_checkout_read = on_read
    _runner, outcome, _store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert "配送服务" in outcome.message
    assert session.submit_calls == 0


def test_payment_origin_mismatch_stops_before_submit(tmp_path: Path) -> None:
    session = FakeSession(
        page=PageKind.CHECKOUT,
        checkout=make_checkout(payment_origin="https://pay.example.invalid"),
    )
    _runner, outcome, _store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.FAILED
    assert session.submit_calls == 0


def test_blocked_page_stops_without_retry(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.BLOCKED)
    _runner, outcome, _store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNSUPPORTED
    assert session.submit_calls == 0


def test_checkout_read_failure_is_bounded_then_stops(tmp_path: Path) -> None:
    session = FakeSession(
        page=PageKind.CHECKOUT,
        checkout=make_checkout(status=ReadStatus.ERROR),
    )
    _runner, outcome, _store, _events = run_runner(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        preparation_timeout=0.5,
    )
    assert outcome.phase is TaskPhase.MISSED
    assert session.submit_calls == 0


def test_checkout_content_change_during_window_stops(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CHECKOUT)
    calls = {"n": 0}
    def on_read(s: FakeSession) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            s.checkout = make_checkout(lines=(make_line(quantity=1),))
    session.on_checkout_read = on_read
    _runner, outcome, _store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.submit_calls == 0


def test_unknown_payment_result_blocks_and_does_not_resubmit(tmp_path: Path) -> None:
    session = FakeSession(
        page=PageKind.CHECKOUT,
        order_results=[
            OrderResult(status=OrderStatus.UNKNOWN, evidence=f"no confirmation {index}")
            for index in range(100)
        ],
    )
    clock = FakeClock()
    _runner, outcome, store, _events = run_runner(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.submit_calls == 1
    loaded = store.load()
    assert loaded.blocking
    assert loaded.record is not None
    assert loaded.record.state is SubmissionState.UNKNOWN


class ClosedResultSession(FakeSession):
    def read_order_result(self) -> OrderResult:
        raise RuntimeError("page closed")


def test_result_page_close_keeps_unknown_and_does_not_resubmit(tmp_path: Path) -> None:
    session = ClosedResultSession(page=PageKind.CHECKOUT)
    _runner, outcome, store, _events = run_runner(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
    )
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.submit_calls == 1
    assert store.load().blocking


def test_declined_payment_does_not_resubmit(tmp_path: Path) -> None:
    session = FakeSession(
        page=PageKind.CHECKOUT,
        order_results=[OrderResult(status=OrderStatus.DECLINED, evidence="declined")],
    )
    _runner, outcome, store, _events = run_runner(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.FAILED
    assert session.submit_calls == 1
    assert store.load().record.state is SubmissionState.DECLINED


def test_stop_before_submit_prevents_click(tmp_path: Path) -> None:
    session = FakeSession(page=PageKind.CHECKOUT)
    cancel = threading.Event()
    cancel.set()
    _runner, outcome, _store, _events = run_runner(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        cancel=cancel,
    )
    assert outcome.phase is TaskPhase.STOPPED
    assert session.submit_calls == 0


def test_stop_after_submit_keeps_unknown_block(tmp_path: Path) -> None:
    session = FakeSession(
        page=PageKind.CHECKOUT,
        order_results=[OrderResult(status=OrderStatus.PENDING)],
    )
    cancel = threading.Event()
    def on_submit(_s: FakeSession) -> None:
        cancel.set()
    session.on_submit = on_submit
    _runner, outcome, store, _events = run_runner(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        cancel=cancel,
    )
    assert outcome.phase is TaskPhase.STOPPED
    assert session.submit_calls == 1
    assert store.load().blocking


def test_describe_window_immediate_preparation() -> None:
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    summary = describe_window(ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60), now=now)
    assert not summary.started
    assert summary.deadline_utc is None
    assert "准备就绪后开始" in summary.text


def test_describe_window_scheduled_and_active() -> None:
    start = parse_beijing_datetime("2026-11-06 14:02:10")
    schedule = ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=start, window_seconds=60)
    summary = describe_window(schedule, now=datetime(2026, 11, 6, 5, 0, tzinfo=UTC))
    assert summary.deadline_utc == start + timedelta(seconds=60)
    assert "计划起点" in summary.text
    from tests.support import window

    active = window(start=start, seconds=60, mode=ScheduleMode.SCHEDULED, effective_start=start)
    active_summary = describe_window(schedule, now=start + timedelta(seconds=10), window=active)
    assert active_summary.started
    assert active_summary.remaining_seconds == 50
    assert "提交截止" in active_summary.text


def test_single_instance_lock(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "app.lock")
    assert lock.acquire()
    second = SingleInstanceLock(tmp_path / "app.lock")
    assert not second.acquire()
    lock.release()
    assert second.acquire()
    second.release()


class GateSession(FakeSession):
    def __init__(self) -> None:
        super().__init__(page=PageKind.OTHER)
        self.gate = threading.Event()

    def page_kind(self) -> PageKind:
        if not self.gate.is_set():
            self.gate.wait(timeout=5)
        return PageKind.OTHER


def test_task_manager_rejects_start_while_old_thread_is_alive(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / "run-state.json")
    manager = TaskManager(store=store, clock=FakeClock(), events=queue.Queue())
    session = GateSession()
    snapshot = make_snapshot(config=make_config())
    first = manager.start(snapshot, session)
    assert first.ok
    manager.stop()
    second = manager.start(snapshot, session)
    assert not second.ok
    session.gate.set()
    assert manager.join(timeout=5)
    third = manager.start(snapshot, session)
    assert third.ok
    manager.stop()
    manager.join(timeout=5)


def test_single_instance_lock_across_processes(tmp_path: Path) -> None:
    import subprocess
    import sys

    lock_path = tmp_path / "app.lock"
    lock = SingleInstanceLock(lock_path)
    assert lock.acquire()
    try:
        code = (
            "import sys;"
            "from pathlib import Path;"
            "from topps_auto_bot.runner import SingleInstanceLock;"
            f"sys.exit(0 if SingleInstanceLock(Path({str(lock_path)!r})).acquire() else 7)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PYTHONPATH": str(Path.cwd() / "src")},
        )
        assert result.returncode == 7
    finally:
        lock.release()


def test_performance_summary() -> None:
    from topps_auto_bot.runner import PerformanceSample

    now = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    samples = [
        PerformanceSample(PreparedLevel.CART_READY, "x", now, now, value)
        for value in (10.0, 20.0, 30.0, 40.0, 100.0)
    ]
    summary = performance_summary(samples, PreparedLevel.CART_READY)
    assert summary["count"] == 5
    assert summary["p95_ms"] is not None
    assert summary["max_ms"] == 100.0
