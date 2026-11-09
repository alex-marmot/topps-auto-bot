from __future__ import annotations

import json
import queue
import threading
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support import (
    FakeClock,
    FakeFullSession,
    make_cart,
    make_config,
    make_preset,
    make_snapshot,
    shipping_snapshot,
)
from topps_auto_bot.browser import TrustedSourcePolicy
from topps_auto_bot.models import (
    AddToCartState,
    PreparedLevel,
    ScheduleMode,
    ScheduleSettings,
    SubmissionState,
    TaskPhase,
    UTC,
)
from topps_auto_bot.purchase import build_submission_record
from topps_auto_bot.runner import FullFlowRunner
from topps_auto_bot.state import (
    AddToCartRecord,
    RunStateBlocked,
    RunStateStatus,
    RunStateStore,
    RunStateStoreError,
    build_add_to_cart_record,
)


def add_record(
    *,
    task_id: str = "task-1",
    now: datetime | None = None,
    attempt_seq: int = 1,
) -> AddToCartRecord:
    now = now or datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    return build_add_to_cart_record(
        task_id=task_id,
        target=make_preset(),
        now=now,
        attempt_seq=attempt_seq,
    )


def purchase_record(now: datetime | None = None):
    now = now or datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    return build_submission_record(
        task_id="task-1",
        preset=make_preset(),
        order_limit_usd=Decimal("70.00"),
        prepared_level=PreparedLevel.CHECKOUT_READY,
        planned_start_utc=None,
        deadline_utc=now,
        shipping=shipping_snapshot(),
        total_usd=Decimal("55.00"),
        now=now,
    )


def make_runner(store: RunStateStore, session: FakeFullSession, clock: FakeClock) -> FullFlowRunner:
    return FullFlowRunner(
        snapshot=make_snapshot(
            config=make_config(),
            schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60),
        ),
        session=session,
        store=store,
        clock=clock,
        events=queue.Queue(),
        cancel_event=threading.Event(),
        policy=TrustedSourcePolicy(),
    )


def test_missing_state_is_not_blocking(tmp_path: Path) -> None:
    loaded = RunStateStore(tmp_path / "run-state.json").load()
    assert loaded.status is RunStateStatus.MISSING
    assert not loaded.blocking


def test_add_in_flight_blocks_and_confirmed_does_not(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    record = add_record(now=now)
    store.begin_add_to_cart(record, now=now)
    loaded = store.load()
    assert loaded.blocking
    assert loaded.add_to_cart is not None
    assert loaded.add_to_cart.state is AddToCartState.IN_FLIGHT
    with pytest.raises(RunStateBlocked):
        store.begin_add_to_cart(add_record(task_id="task-2", now=now), now=now)
    confirmed = replace(record, state=AddToCartState.CONFIRMED)
    store.update_add_to_cart(confirmed, now=now)
    assert not store.load().blocking


def test_purchase_requires_confirmed_add_and_blocks_until_resolved(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    record = add_record(now=now)
    store.begin_add_to_cart(record, now=now)
    with pytest.raises(RunStateBlocked):
        store.begin_purchase(purchase_record(now=now), now=now)
    store.update_add_to_cart(replace(record, state=AddToCartState.CONFIRMED), now=now)
    store.begin_purchase(purchase_record(now=now), now=now)
    assert store.load().blocking
    resolved = store.resolve_purchase(
        state=SubmissionState.NOT_SUBMITTED,
        note="用户核对后确认未形成订单",
        now=now,
    )
    assert resolved.state is SubmissionState.NOT_SUBMITTED
    assert not store.load().blocking


def test_unknown_add_can_be_resolved_but_not_auto_cleared(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    record = add_record(now=now)
    store.begin_add_to_cart(record, now=now)
    store.update_add_to_cart(replace(record, state=AddToCartState.UNKNOWN), now=now)
    assert store.load().blocking
    with pytest.raises(RunStateBlocked):
        store.begin_add_to_cart(add_record(task_id="task-2", now=now), now=now)
    resolved = store.resolve_add_to_cart(
        state=AddToCartState.NOT_SENT,
        note="用户在官网核对后记录",
        now=now,
    )
    assert resolved.state is AddToCartState.NOT_SENT
    assert not store.load().blocking


def test_old_schema_one_purchase_record_is_preserved_and_blocks(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    old = purchase_record().to_dict()
    path.write_text(json.dumps(old), encoding="utf-8")
    store = RunStateStore(path)
    loaded = store.load()
    assert loaded.status is RunStateStatus.OK
    assert loaded.flow_type == "legacy_prepared"
    assert loaded.purchase is not None
    assert loaded.blocking
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    store.resolve_purchase(
        state=SubmissionState.NOT_SUBMITTED,
        note="旧流程未决记录人工核对",
        now=now,
    )
    assert not store.load().blocking
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["flow_type"] == "legacy_prepared"


def test_corrupt_or_unsupported_state_blocks_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    store = RunStateStore(path)
    path.write_text("{broken", encoding="utf-8")
    assert store.load().blocking
    with pytest.raises(RunStateBlocked):
        store.begin_add_to_cart(add_record(), now=datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC))
    assert path.read_text(encoding="utf-8") == "{broken"
    path.write_text('{"schema_version": 99}', encoding="utf-8")
    loaded = store.load()
    assert loaded.status is RunStateStatus.UNSUPPORTED_VERSION
    assert loaded.blocking


def test_add_record_contains_no_sensitive_fields(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    store = RunStateStore(path)
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    store.begin_add_to_cart(add_record(now=now), now=now)
    text = path.read_text(encoding="utf-8")
    config = make_config()
    assert config.card.number not in text
    assert config.card.cvv not in text
    assert config.customer.address_line1 not in text
    assert "token" not in text.casefold()


def test_add_attempts_are_journaled_with_independent_sequences(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    first = add_record(now=now, attempt_seq=1)
    store.begin_add_to_cart(first, now=now)
    store.update_add_to_cart(replace(first, state=AddToCartState.NOT_SENT), now=now)
    second = add_record(now=now, attempt_seq=2)
    store.begin_add_to_cart(second, now=now)
    store.update_add_to_cart(replace(second, state=AddToCartState.CONFIRMED), now=now)
    loaded = store.load()
    assert loaded.add_to_cart is not None
    assert loaded.add_to_cart.attempt_seq == 2
    assert [attempt.attempt_seq for attempt in loaded.add_to_cart_attempts] == [1, 2]
    assert [attempt.state for attempt in loaded.add_to_cart_attempts] == [
        AddToCartState.NOT_SENT,
        AddToCartState.CONFIRMED,
    ]
    assert not loaded.blocking


def test_pending_history_without_current_record_blocks_reloaded_runner(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    clock = FakeClock()
    RunStateStore(path).begin_add_to_cart(add_record(task_id="old"), now=clock.now())
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["add_to_cart"]
    damaged = json.dumps(payload)
    path.write_text(damaged, encoding="utf-8")

    store = RunStateStore(path)
    session = FakeFullSession()
    outcome = make_runner(store, session, clock).run()

    assert outcome.phase is TaskPhase.BLOCKED
    assert session.add_calls == session.submit_calls == 0
    assert store.load().status is RunStateStatus.CORRUPT
    with pytest.raises(RunStateBlocked):
        store.begin_add_to_cart(add_record(task_id="new"), now=clock.now())
    assert path.read_text(encoding="utf-8") == damaged


def test_payment_update_preserves_journal_damaged_during_submission(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    store = RunStateStore(path)
    clock = FakeClock()
    session = FakeFullSession()
    session.on_add = lambda browser: setattr(browser, "cart", make_cart())
    damaged = '{"schema_version": 2,'

    def damage_state(_session) -> None:
        path.write_text(damaged, encoding="utf-8")

    session.on_submit = damage_state
    outcome = make_runner(store, session, clock).run()

    assert outcome.phase is TaskPhase.FAILED
    assert session.submit_calls == 1
    assert path.read_text(encoding="utf-8") == damaged
    next_session = FakeFullSession()
    assert make_runner(RunStateStore(path), next_session, clock).run().phase is TaskPhase.BLOCKED
    assert next_session.add_calls == next_session.submit_calls == 0


@pytest.mark.parametrize(
    "damage",
    ["missing-purchase", "null-history", "empty-history", "invalid-entry", "duplicate", "mismatch", "older-pending"],
)
def test_damaged_journal_blocks_without_rewriting(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "run-state.json"
    now = FakeClock().now()
    store = RunStateStore(path)
    record = add_record()
    store.begin_add_to_cart(record, now=now)
    store.update_add_to_cart(replace(record, state=AddToCartState.CONFIRMED), now=now)
    if damage == "missing-purchase":
        store.begin_purchase(purchase_record(now), now=now)
    payload = json.loads(path.read_text(encoding="utf-8"))
    history = payload["add_to_cart_attempts"]
    if damage == "missing-purchase":
        del payload["purchase"]
    elif damage == "null-history":
        payload["add_to_cart_attempts"] = None
    elif damage == "empty-history":
        history.clear()
    elif damage == "invalid-entry":
        history.insert(0, None)
    elif damage == "duplicate":
        history.insert(0, history[-1])
    elif damage == "mismatch":
        history[-1]["state"] = AddToCartState.UNKNOWN
    elif damage == "older-pending":
        history.insert(0, add_record(task_id="old").to_dict())
    damaged = json.dumps(payload)
    path.write_text(damaged, encoding="utf-8")

    loaded = RunStateStore(path).load()
    assert loaded.status is RunStateStatus.CORRUPT
    assert loaded.blocking
    with pytest.raises(RunStateBlocked):
        store.begin_add_to_cart(add_record(task_id="new"), now=now)
    assert path.read_text(encoding="utf-8") == damaged


@pytest.mark.parametrize("damaged", [b'{"schema_version": 2, "schema_version": 2}', b"\xff"])
def test_duplicate_json_fields_and_invalid_encoding_are_corrupt(tmp_path: Path, damaged: bytes) -> None:
    path = tmp_path / "run-state.json"
    path.write_bytes(damaged)
    loaded = RunStateStore(path).load()
    assert loaded.status is RunStateStatus.CORRUPT
    assert loaded.blocking
    assert path.read_bytes() == damaged


def test_legacy_current_add_without_history_or_sequence_remains_resolvable(tmp_path: Path) -> None:
    path = tmp_path / "run-state.json"
    now = FakeClock().now()
    RunStateStore(path).begin_add_to_cart(add_record(), now=now)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["add_to_cart_attempts"]
    del payload["add_to_cart"]["attempt_seq"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = RunStateStore(path)
    loaded = store.load()
    assert loaded.status is RunStateStatus.OK
    assert loaded.blocking
    assert loaded.add_to_cart is not None
    assert loaded.add_to_cart.attempt_seq == 1
    resolved = store.resolve_add_to_cart(state=AddToCartState.NOT_SENT, note="用户已核对", now=now)
    reloaded = RunStateStore(path).load()
    assert reloaded.status is RunStateStatus.OK
    assert not reloaded.blocking
    assert reloaded.add_to_cart_attempts == (resolved,)


def test_old_attempt_update_cannot_overwrite_current_pending_attempt(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = FakeClock().now()
    first = add_record(attempt_seq=1)
    store.begin_add_to_cart(first, now=now)
    first = store.update_add_to_cart(replace(first, state=AddToCartState.NOT_SENT), now=now)
    second = add_record(attempt_seq=2)
    store.begin_add_to_cart(second, now=now)

    with pytest.raises(RunStateStoreError):
        store.update_add_to_cart(first, now=now)

    reloaded = RunStateStore(store.path).load()
    assert reloaded.status is RunStateStatus.OK
    assert reloaded.blocking
    assert reloaded.add_to_cart == second


def test_only_manual_resolution_can_release_unknown_add(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "run-state.json")
    now = FakeClock().now()
    record = add_record()
    store.begin_add_to_cart(record, now=now)
    store.update_add_to_cart(replace(record, state=AddToCartState.UNKNOWN), now=now)

    with pytest.raises(RunStateStoreError):
        store.update_add_to_cart(replace(record, state=AddToCartState.NOT_SENT), now=now)
    assert RunStateStore(store.path).load().blocking
