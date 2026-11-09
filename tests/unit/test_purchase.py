from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support import (
    make_cart,
    make_checkout,
    make_config,
    make_line,
    make_preset,
    shipping_snapshot,
)
from topps_auto_bot.models import (
    FormState,
    OrderResult,
    OrderStatus,
    PreparedLevel,
    ReadStatus,
    ShippingOption,
    SubmissionState,
    UTC,
)
from topps_auto_bot.purchase import (
    SubmissionBlocked,
    SubmissionLoadStatus,
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


def test_exact_cart_match() -> None:
    preset = make_preset()
    check = match_single_target((make_line(),), preset, currency="USD")
    assert check.ok
    assert check.line is not None


@pytest.mark.parametrize(
    "line",
    [
        make_line(quantity=1),
        make_line(quantity=3),
        make_line(variant="Retail Box"),
        make_line(title="Other Card"),
        make_line(unit_price="25.01"),
    ],
)
def test_cart_mismatch_rejected(line) -> None:
    preset = make_preset()
    check = match_single_target((line,), preset, currency="USD")
    assert not check.ok


def test_cart_extra_duplicate_and_empty_rejected() -> None:
    preset = make_preset()
    assert not match_single_target((), preset, currency="USD").ok
    assert not match_single_target((make_line(), make_line()), preset, currency="USD").ok


def test_unknown_price_and_non_usd_rejected() -> None:
    preset = make_preset()
    check = match_single_target((make_line(unit_price=None),), preset, currency="USD")
    assert not check.ok
    assert "单价" in (check.reason or "")
    assert not match_single_target((make_line(),), preset, currency="EUR").ok


def test_cart_readiness_requires_confirmed_login() -> None:
    preset = make_preset()
    assert evaluate_cart(make_cart(logged_in=None), preset).level is PreparedLevel.NONE
    assert evaluate_cart(make_cart(logged_in=False), preset).level is PreparedLevel.NONE
    ready = evaluate_cart(make_cart(), preset)
    assert ready.level is PreparedLevel.CART_READY
    assert "锁定库存" in " ".join(ready.reasons)


def test_cart_error_statuses() -> None:
    preset = make_preset()
    for status in (
        ReadStatus.EMPTY,
        ReadStatus.NOT_LOGGED_IN,
        ReadStatus.SESSION_LOST,
        ReadStatus.BLOCKED,
        ReadStatus.ERROR,
    ):
        assert evaluate_cart(make_cart(status=status), preset).level is PreparedLevel.NONE


def test_checkout_ready_and_free_shipping() -> None:
    preset = make_preset()
    free_shipping = (
        ShippingOption("standard", "Standard", Decimal("0.00"), True),
    )
    checkout = make_checkout(subtotal="40.00", tax="5.00", shipping_total="0.00", total="45.00", shipping_options=free_shipping)
    readiness = evaluate_checkout(checkout, preset, Decimal("70.00"))
    assert readiness.level is PreparedLevel.CHECKOUT_READY
    assert readiness.details["shipping"]["amount_usd"] == "0.00"


def test_checkout_ready_when_final_button_disabled() -> None:
    readiness = evaluate_checkout(make_checkout(final_submit_enabled=False), make_preset(), Decimal("70.00"))
    assert readiness.level is PreparedLevel.CHECKOUT_READY


@pytest.mark.parametrize(
    "checkout",
    [
        make_checkout(form=FormState(contact_complete=False, card_complete=True)),
        make_checkout(form=FormState(contact_complete=True, card_complete=False)),
        make_checkout(form=FormState(contact_complete=True, card_complete=True, errors=("invalid card",))),
        make_checkout(shipping_options=()),
        make_checkout(
            shipping_options=(
                ShippingOption("standard", "Standard", Decimal("10.00"), True),
                ShippingOption("express", "Express", Decimal("25.00"), True),
            )
        ),
        make_checkout(shipping_total=None),
        make_checkout(tax=None),
        make_checkout(total="54.99"),
        make_checkout(total="70.01", subtotal="55.01"),
        make_checkout(currency="EUR"),
        make_checkout(lines=(make_line(quantity=1),)),
    ],
)
def test_checkout_not_ready_cases(checkout) -> None:
    readiness = evaluate_checkout(checkout, make_preset(), Decimal("70.00"))
    assert readiness.level is PreparedLevel.NONE


def test_checkout_exact_budget_boundary() -> None:
    checkout = make_checkout(subtotal="40.00", tax="5.00", shipping_total="10.00", total="55.00")
    assert evaluate_checkout(checkout, make_preset(), Decimal("55.00")).level is PreparedLevel.CHECKOUT_READY
    assert evaluate_checkout(checkout, make_preset(), Decimal("54.99")).level is PreparedLevel.NONE


def test_default_shipping_must_be_uniquely_selected() -> None:
    checkout = make_checkout(
        shipping_options=(
            ShippingOption("standard", "Standard", Decimal("10.00"), False),
            ShippingOption("express", "Express", Decimal("25.00"), False),
        )
    )
    assert freeze_shipping(checkout)[0] is None
    snapshot, reason = freeze_shipping(make_checkout())
    assert snapshot is not None
    assert reason is None


def test_shipping_snapshot_changes_stop() -> None:
    snapshot = shipping_snapshot()
    same = make_checkout()
    assert verify_shipping_snapshot(same, snapshot) is None
    changed_service = make_checkout(
        shipping_options=(
            ShippingOption("standard", "White Glove", Decimal("10.00"), True),
        )
    )
    assert "配送服务" in (verify_shipping_snapshot(changed_service, snapshot) or "")
    changed_amount = make_checkout(
        subtotal="40.00",
        tax="5.00",
        shipping_total="12.00",
        total="57.00",
        shipping_options=(ShippingOption("standard", "Standard", Decimal("12.00"), True),),
    )
    assert "运费" in (verify_shipping_snapshot(changed_amount, snapshot) or "")


def test_verify_before_submit_requires_enabled_button() -> None:
    snapshot = shipping_snapshot()
    preset = make_preset()
    assert (
        verify_before_submit(make_checkout(final_submit_enabled=False), preset, Decimal("70.00"), snapshot).level
        is PreparedLevel.NONE
    )
    assert (
        verify_before_submit(make_checkout(final_submit_enabled=True), preset, Decimal("70.00"), snapshot).level
        is PreparedLevel.CHECKOUT_READY
    )


def test_classify_result_requires_matching_order_and_accepted_payment() -> None:
    preset = make_preset()
    success = OrderResult(
        status=OrderStatus.CONFIRMED,
        order_id="ORDER-1",
        payment_state="paid",
        lines=(make_line(),),
    )
    assert classify_order_result(success, preset)[0] is SubmissionState.SUCCEEDED
    no_id = OrderResult(status=OrderStatus.CONFIRMED, payment_state="paid", matches_target=True)
    assert classify_order_result(no_id, preset)[0] is SubmissionState.UNKNOWN
    mismatch = OrderResult(
        status=OrderStatus.CONFIRMED,
        order_id="ORDER-2",
        payment_state="paid",
        lines=(make_line(quantity=1),),
    )
    assert classify_order_result(mismatch, preset)[0] is SubmissionState.UNKNOWN
    no_payment = OrderResult(
        status=OrderStatus.CONFIRMED,
        order_id="ORDER-3",
        payment_state=None,
        lines=(make_line(),),
    )
    assert classify_order_result(no_payment, preset)[0] is SubmissionState.UNKNOWN
    not_paid = OrderResult(
        status=OrderStatus.CONFIRMED,
        order_id="ORDER-4",
        payment_state="not paid",
        lines=(make_line(),),
    )
    assert classify_order_result(not_paid, preset)[0] is SubmissionState.UNKNOWN
    declined = OrderResult(status=OrderStatus.DECLINED)
    assert classify_order_result(declined, preset)[0] is SubmissionState.DECLINED
    pending = OrderResult(status=OrderStatus.PENDING)
    assert classify_order_result(pending, preset)[0] is SubmissionState.UNKNOWN


def test_submission_store_blocks_and_resolves(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / "run-state.json")
    assert store.load().status is SubmissionLoadStatus.MISSING
    config = make_config()
    preset = config.selected_preset()
    assert preset is not None
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    record = build_submission_record(
        task_id="task-1",
        preset=preset,
        order_limit_usd=Decimal("70.00"),
        prepared_level=PreparedLevel.CHECKOUT_READY,
        planned_start_utc=None,
        deadline_utc=now,
        shipping=shipping_snapshot(),
        total_usd=Decimal("55.00"),
        now=now,
    )
    store.begin(record)
    loaded = store.load()
    assert loaded.blocking
    with pytest.raises(SubmissionBlocked):
        store.begin(record)
    resolved = store.resolve(
        state=SubmissionState.NOT_SUBMITTED,
        note="用户核对后确认未形成订单",
        now=now,
    )
    assert resolved.state is SubmissionState.NOT_SUBMITTED
    assert not store.load().blocking


def test_submission_record_contains_no_sensitive_fields(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / "run-state.json")
    config = make_config()
    preset = config.selected_preset()
    assert preset is not None
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    record = build_submission_record(
        task_id="task-1",
        preset=preset,
        order_limit_usd=Decimal("70.00"),
        prepared_level=PreparedLevel.CART_READY,
        planned_start_utc=None,
        deadline_utc=now,
        shipping=shipping_snapshot(),
        total_usd=Decimal("55.00"),
        now=now,
    )
    store.begin(record)
    text = (tmp_path / "run-state.json").read_text(encoding="utf-8")
    assert config.card.number not in text
    assert config.card.cvv not in text
    assert config.customer.address_line1 not in text
    payload = json.loads(text)
    assert "address" not in text.casefold()
    assert "card" not in payload
    assert "token" not in text.casefold()


def test_corrupt_or_future_submission_record_blocks(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    store = SubmissionStore(path)
    path.write_text("{broken", encoding="utf-8")
    assert store.load().blocking
    assert path.read_text(encoding="utf-8") == "{broken"
    path.write_text('{"schema_version": 99}', encoding="utf-8")
    loaded = store.load()
    assert loaded.status is SubmissionLoadStatus.UNSUPPORTED_VERSION
    assert loaded.blocking


def test_begin_refuses_to_overwrite_corrupt_record(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    path.write_text("{broken", encoding="utf-8")
    store = SubmissionStore(path)
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    record = build_submission_record(
        task_id="task-1",
        preset=make_preset(),
        order_limit_usd=Decimal("70.00"),
        prepared_level=PreparedLevel.CART_READY,
        planned_start_utc=None,
        deadline_utc=now,
        shipping=shipping_snapshot(),
        total_usd=Decimal("55.00"),
        now=now,
    )
    with pytest.raises(SubmissionBlocked):
        store.begin(record)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_submission_record_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    store = SubmissionStore(path)
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    preset = make_preset()
    record = build_submission_record(
        task_id="task-1",
        preset=preset,
        order_limit_usd=Decimal("70.00"),
        prepared_level=PreparedLevel.CART_READY,
        planned_start_utc=None,
        deadline_utc=now,
        shipping=shipping_snapshot(),
        total_usd=Decimal("55.00"),
        now=now,
    )
    payload = record.to_dict()
    payload["card_number"] = "4111111111111111"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.load().status is SubmissionLoadStatus.CORRUPT
