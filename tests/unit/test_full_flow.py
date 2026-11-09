from __future__ import annotations

import queue
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support import (
    FakeClock,
    FakeFullSession,
    empty_cart,
    make_cart,
    make_checkout,
    make_config,
    make_line,
    make_preset,
    make_product_page,
    make_snapshot,
)
from topps_auto_bot.browser import TrustedSourcePolicy
from topps_auto_bot.models import (
    ActionResult,
    AddToCartState,
    LoginRead,
    LoginStatus,
    OrderResult,
    OrderStatus,
    PreparedLevel,
    ReadStatus,
    ScheduleMode,
    ScheduleSettings,
    SubmissionState,
    TaskPhase,
    UTC,
)
from topps_auto_bot.runner import FullFlowRunner
from topps_auto_bot.state import AddToCartRecord, RunStateStatus, RunStateStore, build_add_to_cart_record


def run_full(
    *,
    session: FakeFullSession,
    clock: FakeClock,
    tmp_path: Path,
    snapshot=None,
    store: RunStateStore | None = None,
    cancel: threading.Event | None = None,
    preparation_timeout: float = 5.0,
    manual_login_timeout: float = 600.0,
    login_result_timeout: float = 30.0,
    auto_login: bool = False,
):
    snapshot = snapshot or make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60),
    )
    store = store or RunStateStore(tmp_path / "run-state.json")
    runner = FullFlowRunner(
        snapshot=replace(snapshot, auto_login=auto_login),
        session=session,
        store=store,
        clock=clock,
        events=queue.Queue(),
        cancel_event=cancel or threading.Event(),
        policy=TrustedSourcePolicy(allow_loopback_http=True),
        preparation_timeout_seconds=preparation_timeout,
        result_timeout_seconds=60.0,
        poll_interval_seconds=0.1,
        result_poll_interval_seconds=1.0,
        manual_login_timeout_seconds=manual_login_timeout,
        login_result_timeout_seconds=login_result_timeout,
    )
    return runner.run(), store


def test_full_flow_happy_path_from_empty_cart(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.add_calls == 1
    assert session.submit_calls == 1
    loaded = store.load()
    assert loaded.add_to_cart is not None
    assert loaded.add_to_cart.state is AddToCartState.CONFIRMED
    assert loaded.purchase is not None
    assert loaded.purchase.state is SubmissionState.SUCCEEDED
    assert loaded.purchase.deadline_utc is None
    assert loaded.purchase.submitted_at_utc is not None
    assert not loaded.blocking


def test_non_empty_cart_stops_before_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=make_cart())
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0
    assert store.load().status is RunStateStatus.MISSING


def test_login_not_confirmed_does_not_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), logged_in=False)
    session.login_result = ActionResult(False, "login unavailable", attempted=False)
    session.on_login = lambda s: setattr(
        s, "login_state", LoginRead(status=LoginStatus.NOT_LOGGED_IN, error="login unavailable")
    )
    outcome, _store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        preparation_timeout=0.5,
    )
    assert outcome.phase is TaskPhase.MISSED
    assert session.add_calls == 0
    assert session.login_calls == 0


def test_login_reuses_matching_session_and_records_timing(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.login_calls == 0
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "REUSED"
    assert any(sample.action == "login_prepare_reused" for sample in outcome.performance)


def test_login_auto_success_before_full_flow(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), logged_in=False)
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, auto_login=True)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.login_calls == 1
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "AUTO"
    assert session.add_calls == 1


def test_login_auto_failure_then_manual_success_without_repeat(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), logged_in=False)
    manual = {"enabled": False}

    def fail_auto(s: FakeFullSession) -> None:
        s.logged_in = False
        s.login_state = LoginRead(status=LoginStatus.NOT_LOGGED_IN, error="invalid credentials")

    def complete_manual(s: FakeFullSession) -> None:
        if s.login_calls and s.login_state_reads >= 3:
            s.logged_in = True
            s.login_state = LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity=s.login_credentials.email,
            )

    session.on_login = fail_auto
    session.on_login_state_read = complete_manual
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, auto_login=True)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.login_calls == 1
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "MANUAL"
    assert session.add_calls == 1


def test_auto_login_timeout_then_manual_success(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), logged_in=False)

    def fail_auto(s: FakeFullSession) -> None:
        s.login_state = LoginRead(status=LoginStatus.NOT_LOGGED_IN)

    def complete_manual(s: FakeFullSession) -> None:
        if s.login_state_reads >= 8:
            s.logged_in = True
            s.login_state = LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity=s.login_credentials.email,
            )

    session.on_login = fail_auto
    session.on_login_state_read = complete_manual
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        login_result_timeout=0.5,
        auto_login=True,
    )
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.login_calls == 1
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "MANUAL"


@pytest.mark.parametrize("identity", [None, "different@example.test"])
def test_default_reuses_observed_login_without_matching_saved_email(tmp_path, identity):
    session = FakeFullSession(cart=empty_cart())
    session.login_state = LoginRead(status=LoginStatus.LOGGED_IN, account_identity=identity)
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _ = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.login_calls == 0
    assert outcome.login_preparation.mode == "REUSED"


def test_login_challenge_goes_manual_without_credentials(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), logged_in=False)
    session.login_state = LoginRead(status=LoginStatus.CHALLENGE, challenge=True, error="verification required")

    def complete_manual(s: FakeFullSession) -> None:
        if s.login_state_reads >= 2:
            s.logged_in = True
            s.login_state = LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity=s.login_credentials.email,
            )

    session.on_login_state_read = complete_manual
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.login_calls == 0
    assert session.add_calls == 1


def test_manual_login_cancel_does_not_add(tmp_path: Path) -> None:
    cancel = threading.Event()
    session = FakeFullSession(cart=empty_cart(), logged_in=False)

    def fail_auto(s: FakeFullSession) -> None:
        s.login_state = LoginRead(status=LoginStatus.NOT_LOGGED_IN, error="invalid credentials")
        cancel.set()

    session.on_login_state_read = fail_auto
    outcome, _store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        cancel=cancel,
    )
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0
    assert session.login_calls == 0


def test_manual_login_timeout_does_not_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), logged_in=False)
    session.on_login = lambda s: setattr(
        s, "login_state", LoginRead(status=LoginStatus.NOT_LOGGED_IN, error="invalid credentials")
    )
    outcome, _store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        preparation_timeout=3.0,
        manual_login_timeout=0.5,
    )
    assert outcome.phase is TaskPhase.MISSED
    assert session.add_calls == 0
    assert session.login_calls == 0


def test_account_change_during_execution_stops_before_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    original_read = session.read_product_page
    reads = {"n": 0}

    def read_product():
        reads["n"] += 1
        if reads["n"] == 1:
            session.login_state = LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity="other@example.test",
            )
        return original_read()

    session.read_product_page = read_product  # type: ignore[assignment]
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0
    assert session.login_calls == 0


def test_quantity_setting_failure_stops_before_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())

    def fail_quantity(quantity: int) -> ActionResult:
        session.quantity_calls += 1
        return ActionResult(False, "quantity rejected", attempted=True)

    session.set_quantity = fail_quantity  # type: ignore[assignment]
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0


def test_variant_is_selected_and_requested_quantity_used(tmp_path: Path) -> None:
    config = make_config(presets=(make_preset(variant="Retail Box"),))
    snapshot = make_snapshot(
        config=config,
        schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60),
    )
    session = FakeFullSession(
        cart=empty_cart(),
        checkout=make_checkout(lines=(make_line(variant="Retail Box"),)),
        order_results=[
            OrderResult(
                status=OrderStatus.CONFIRMED,
                order_id="ORDER-RETAIL",
                payment_state="paid",
                lines=(make_line(variant="Retail Box"),),
            )
        ],
    )
    session.on_add = lambda s: setattr(
        s,
        "cart",
        make_cart(lines=(make_line(variant="Retail Box"),)),
    )
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, snapshot=snapshot)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.select_calls == 1
    assert session.quantity_calls == 1


def test_price_over_limit_stops_before_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), product=make_product_page(unit_price="30.00"))
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0


def test_unknown_price_stops_before_add(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart(), product=make_product_page(unit_price=None))
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0


def test_proven_not_sent_add_is_retried_with_new_attempt(tmp_path: Path) -> None:
    session = FakeFullSession(
        cart=empty_cart(),
        add_result=ActionResult(False, "button unavailable", attempted=False),
    )
    calls = {"n": 0}

    def add_again(s: FakeFullSession) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            s.add_result = ActionResult(True, attempted=True)
            s.cart = make_cart()

    session.on_add = add_again
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.add_calls == 2
    assert store.load().add_to_cart is not None
    assert store.load().add_to_cart.state is AddToCartState.CONFIRMED
    assert store.load().add_to_cart.attempt_seq == 2
    assert session.submit_calls == 1


def test_add_attempted_but_cart_unchanged_is_unknown_and_blocks(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.add_calls == 1
    assert store.load().blocking
    assert store.load().add_to_cart is not None
    assert store.load().add_to_cart.state is AddToCartState.UNKNOWN
    assert session.submit_calls == 0


def test_explicit_temporary_rejection_with_empty_cart_is_retried(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    calls = {"n": 0}

    def add_again(s: FakeFullSession) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            s.add_result = ActionResult(
                True,
                attempted=True,
                confirmed_no_side_effect=True,
                error="temporarily unavailable",
            )
        else:
            s.add_result = ActionResult(True, attempted=True)
            s.cart = make_cart()

    session.on_add = add_again
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.add_calls == 2
    loaded = store.load()
    assert loaded.add_to_cart is not None
    assert loaded.add_to_cart.attempt_seq == 2
    assert loaded.add_to_cart.state is AddToCartState.CONFIRMED


def test_product_read_errors_stop_after_three_attempts(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    reads = {"n": 0}

    def read_product():
        reads["n"] += 1
        return make_product_page(status=ReadStatus.ERROR)

    session.read_product_page = read_product  # type: ignore[assignment]
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert reads["n"] == 3
    assert session.add_calls == 0


def test_add_click_uncertain_blocks_without_retry(tmp_path: Path) -> None:
    session = FakeFullSession(
        cart=empty_cart(),
        add_result=ActionResult(False, "click timeout", uncertain=True, attempted=True),
    )
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.add_calls == 1
    assert store.load().blocking
    assert store.load().add_to_cart is not None
    assert store.load().add_to_cart.state is AddToCartState.UNKNOWN
    assert session.submit_calls == 0


def test_add_second_empty_check_detects_external_cart_change(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    calls = {"n": 0}

    def on_navigate(s: FakeFullSession) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            s.cart = make_cart()

    session.on_navigate_cart = on_navigate
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0
    assert "已有商品" in outcome.message


def test_product_becomes_available_within_window(tmp_path: Path) -> None:
    session = FakeFullSession(
        cart=empty_cart(),
        product=make_product_page(add_to_cart_enabled=False),
    )
    reads = {"n": 0}
    original_read = session.read_product_page

    def read_product():
        reads["n"] += 1
        if reads["n"] == 3:
            session.product = replace(session.product, add_to_cart_enabled=True)
        return original_read()

    session.read_product_page = read_product  # type: ignore[assignment]
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert session.add_calls == 1


def test_partial_cart_after_add_is_unknown_and_blocks(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart(lines=(make_line(quantity=1),)))
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.add_calls == 1
    assert session.submit_calls == 0
    assert store.load().blocking
    assert store.load().add_to_cart is not None
    assert store.load().add_to_cart.state is AddToCartState.UNKNOWN


def test_stop_after_add_leaves_in_flight_block(tmp_path: Path) -> None:
    cancel = threading.Event()
    session = FakeFullSession(cart=empty_cart())

    def on_add(s: FakeFullSession) -> None:
        cancel.set()

    session.on_add = on_add
    outcome, store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        cancel=cancel,
    )
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 1
    assert session.submit_calls == 0
    assert store.load().blocking


def test_shipping_change_after_add_stops_before_payment(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    calls = {"n": 0}
    original_checkout = session.checkout

    def on_checkout_read(s: FakeFullSession) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            from topps_auto_bot.models import ShippingOption

            s.checkout = make_checkout(
                shipping_options=(ShippingOption("express", "Express", Decimal("10.00"), True),)
            )

    session.on_checkout_read = on_checkout_read
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert "配送服务" in outcome.message
    assert session.submit_calls == 0
    assert store.load().add_to_cart is not None
    assert store.load().add_to_cart.state is AddToCartState.CONFIRMED


def test_add_record_save_failure_does_not_click(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    store = RunStateStore(tmp_path / "run-state.json")

    def fail_begin(*args, **kwargs) -> None:
        raise RuntimeError("disk full")

    store.begin_add_to_cart = fail_begin  # type: ignore[assignment]
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, store=store)
    assert outcome.phase is TaskPhase.FAILED
    assert session.add_calls == 0


@pytest.mark.parametrize(("elapsed", "phase"), [(29, TaskPhase.SUCCEEDED), (31, TaskPhase.UNKNOWN)])
def test_add_click_time_counts_toward_result_deadline(tmp_path: Path, elapsed, phase) -> None:
    clock = FakeClock()
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: (setattr(s, "cart", make_cart()), clock.advance(elapsed))
    outcome, store = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is phase
    assert session.add_calls == 1
    assert session.submit_calls == (phase is TaskPhase.SUCCEEDED)
    assert store.load().blocking is (phase is TaskPhase.UNKNOWN)


@pytest.mark.parametrize("lines", [(make_line(quantity=1),), (make_line(), make_line(title="Extra"))])
def test_explicit_rejection_then_empty_then_conflicting_cart_blocks(tmp_path: Path, lines) -> None:
    session = FakeFullSession(cart=empty_cart(), add_result=ActionResult(
        False, "temporary rejection", attempted=True, confirmed_no_side_effect=True,
    ))
    post_add_reads = iter((empty_cart(), make_cart(lines=lines)))
    original_read = session.read_cart
    session.read_cart = lambda: next(post_add_reads, make_cart(lines=lines)) if session.add_calls else original_read()
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.add_calls == 1
    assert session.submit_calls == 0
    reopened = RunStateStore(store.path).load()
    assert reopened.blocking
    assert reopened.add_to_cart.state is AddToCartState.UNKNOWN


@pytest.mark.parametrize("gap", ["total_usd", "final_submit_enabled"])
def test_checkout_wait_is_bounded(tmp_path: Path, gap) -> None:
    clock = FakeClock()
    session = FakeFullSession(cart=empty_cart(), checkout=replace(
        make_checkout(), **{gap: None if gap == "total_usd" else False},
    ))
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    started = clock.now()

    def checkout_read(s):
        # A late successful read must not advance the purchase.
        if clock.now() >= started + timedelta(seconds=31):
            s.checkout = make_checkout()

    session.on_checkout_read = checkout_read
    outcome, _ = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert session.submit_calls == 0


def test_payment_click_time_counts_toward_shared_result_deadline(tmp_path: Path) -> None:
    clock = FakeClock()
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    session.on_submit = lambda s: clock.advance(61)
    outcome, store = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.submit_calls == 1
    assert store.load().blocking


def test_waiting_over_600_seconds_can_still_checkout(tmp_path: Path) -> None:
    clock = FakeClock()
    started = clock.now()
    session = FakeFullSession(cart=empty_cart(), product=make_product_page(add_to_cart_enabled=False))

    def restock(s):
        if (clock.now() - started).total_seconds() > 700:
            s.product = replace(s.product, add_to_cart_enabled=True)

    session.on_product_read = restock
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _ = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert (clock.now() - started).total_seconds() > 700
    assert session.add_calls == session.submit_calls == 1


def test_bank_verification_does_not_reset_payment_deadline(tmp_path: Path) -> None:
    clock = FakeClock()
    session = FakeFullSession(cart=empty_cart(), order_results=[
        OrderResult(status=OrderStatus.VERIFICATION, evidence="银行验证"),
        OrderResult(status=OrderStatus.CONFIRMED, order_id="LATE", payment_state="paid", lines=(make_line(),)),
    ])
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    session.on_submit = lambda s: clock.advance(59)
    outcome, store = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.submit_calls == 1
    assert store.load().blocking
    assert any(event.message == "银行验证" for event in outcome.events)


def test_sold_out_wait_continues_beyond_old_windows_until_stop(tmp_path: Path) -> None:
    cancel = threading.Event()
    clock = FakeClock()
    session = FakeFullSession(
        cart=empty_cart(),
        product=make_product_page(add_to_cart_enabled=False),
    )
    reads = {"n": 0}
    original_read = session.read_product_page

    def read_product():
        reads["n"] += 1
        if reads["n"] >= 50:
            cancel.set()
        return original_read()

    session.read_product_page = read_product  # type: ignore[assignment]
    outcome, store = run_full(
        session=session,
        clock=clock,
        tmp_path=tmp_path,
        cancel=cancel,
    )
    assert outcome.phase is TaskPhase.STOPPED
    assert (clock.current - datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)).total_seconds() > 600
    assert session.add_calls == 0
    assert store.load().status is RunStateStatus.MISSING


def test_old_pending_add_blocks_new_task(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    store.begin_add_to_cart(
        build_add_to_cart_record(task_id="old", target=make_preset(), now=now),
        now=now,
    )
    session = FakeFullSession(cart=empty_cart())
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, store=store)
    assert outcome.phase is TaskPhase.BLOCKED
    assert session.add_calls == 0


def test_old_schema_one_pending_purchase_blocks_new_task(tmp_path: Path) -> None:
    from tests.unit.test_state import purchase_record

    path = tmp_path / "run-state.json"
    path.write_text(__import__("json").dumps(purchase_record().to_dict()), encoding="utf-8")
    session = FakeFullSession(cart=empty_cart())
    outcome, _store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        store=RunStateStore(path),
    )
    assert outcome.phase is TaskPhase.BLOCKED
    assert session.add_calls == 0


def test_purchase_unknown_keeps_add_and_purchase_block(tmp_path: Path) -> None:
    session = FakeFullSession(
        cart=empty_cart(),
        order_results=[OrderResult(status=OrderStatus.UNKNOWN) for _ in range(100)],
    )
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.UNKNOWN
    assert session.add_calls == 1
    assert session.submit_calls == 1
    loaded = store.load()
    assert loaded.blocking
    assert loaded.purchase is not None
    assert loaded.purchase.state is SubmissionState.UNKNOWN


def test_purchase_record_save_failure_does_not_click(tmp_path: Path) -> None:
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    store = RunStateStore(tmp_path / "run-state.json")

    def fail_begin_purchase(*args, **kwargs) -> None:
        raise RuntimeError("disk full")

    store.begin_purchase = fail_begin_purchase  # type: ignore[assignment]
    outcome, _store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, store=store)
    assert outcome.phase is TaskPhase.FAILED
    assert session.add_calls == 1
    assert session.submit_calls == 0
    assert store.load().add_to_cart is not None
    assert store.load().add_to_cart.state is AddToCartState.CONFIRMED


def test_stop_before_add_does_not_click(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    session = FakeFullSession(cart=empty_cart())
    outcome, _store = run_full(
        session=session,
        clock=FakeClock(),
        tmp_path=tmp_path,
        cancel=cancel,
    )
    assert outcome.phase is TaskPhase.STOPPED
    assert session.add_calls == 0


def test_add_conclusion_write_failure_keeps_durable_block(tmp_path):
    store = RunStateStore(tmp_path / "run-state.json")
    original_update = store.update_add_to_cart

    def update(record, *, now):
        if record.state is AddToCartState.CONFIRMED:
            raise OSError("injected write failure")
        return original_update(record, now=now)

    store.update_add_to_cart = update
    session = FakeFullSession(cart=empty_cart())
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _ = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path, store=store)
    assert outcome.phase is TaskPhase.FAILED
    assert session.add_calls == 1
    assert session.submit_calls == 0
    assert RunStateStore(store.path).load().blocking


def test_official_longer_wait_does_not_refresh_or_add_early(tmp_path):
    clock = FakeClock()
    start = clock.now()
    session = FakeFullSession(cart=empty_cart(), product=make_product_page())
    current_product = session.product

    def read_product(s):
        elapsed = (clock.now() - start).total_seconds()
        if elapsed < 44:
            s.product = replace(current_product, status=ReadStatus.WAITING, retry_after_seconds=45)
        elif s.product.status is ReadStatus.WAITING:
            s.product = current_product

    session.on_product_read = read_product
    session.on_add = lambda s: setattr(s, "cart", make_cart())
    outcome, _ = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert (clock.now() - start).total_seconds() >= 44.9
    assert session.refresh_calls == 0
    assert session.add_calls == session.submit_calls == 1


def test_cart_changes_while_reopened_product_waits_for_stock(tmp_path):
    clock = FakeClock()
    started = clock.now()
    session = FakeFullSession(cart=empty_cart(), product=make_product_page(quantity=2))
    product = session.product

    def read_product(s):
        if s.product_opens >= 2 and s.cart_navigations == 2:
            if (clock.now() - started).total_seconds() < 15:
                s.product = replace(product, add_to_cart_enabled=False)
            else:
                s.cart = make_cart(lines=(make_line(title="External item"),))
                s.product = product

    session.on_product_read = read_product
    outcome, store = run_full(session=session, clock=clock, tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert "已有商品" in outcome.message
    assert session.cart_navigations == 3
    assert session.add_calls == session.submit_calls == 0
    assert store.load().status is RunStateStatus.MISSING


@pytest.mark.parametrize("changed_at_final_read", [False, True])
def test_remaining_single_variant_must_match_target(tmp_path, changed_at_final_read):
    session = FakeFullSession(cart=empty_cart(), product=make_product_page(quantity=2))
    retail_only = replace(
        session.product, variants=("Retail Box",), selected_variant="Retail Box", single_variant=True,
    )
    if changed_at_final_read:
        session.on_product_read = lambda s: (
            setattr(s, "product", retail_only) if s.product_reads == 3 else None
        )
    else:
        session.product = retail_only
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert "规格" in outcome.message
    assert session.add_calls == session.submit_calls == 0
    assert store.load().status is RunStateStatus.MISSING


def test_repeated_restock_after_returning_to_product_has_bounded_cart_rechecks(tmp_path):
    session = FakeFullSession(cart=empty_cart(), product=make_product_page(quantity=2))
    product = session.product
    visits = set()

    def read_product(s):
        s.product = replace(
            product, add_to_cart_enabled=s.product_opens == 1 or s.product_opens in visits,
        )
        visits.add(s.product_opens)

    session.on_product_read = read_product
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert "多次核对" in outcome.message
    assert session.product_opens == session.cart_navigations == 4
    assert session.add_calls == session.submit_calls == 0
    assert store.load().status is RunStateStatus.MISSING


def test_page_without_variant_options_does_not_match_named_variant(tmp_path):
    session = FakeFullSession(
        cart=empty_cart(),
        product=make_product_page(variants=(), selected_variant=None, single_variant=True, quantity=2),
    )
    outcome, store = run_full(session=session, clock=FakeClock(), tmp_path=tmp_path)
    assert outcome.phase is TaskPhase.STOPPED
    assert "规格" in outcome.message
    assert session.add_calls == session.submit_calls == 0
    assert store.load().status is RunStateStatus.MISSING
