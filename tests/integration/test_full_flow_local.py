from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path

from tests.support import make_config, make_preset, make_snapshot
from topps_auto_bot.browser import TrustedSourcePolicy
from topps_auto_bot.models import (
    AddToCartState,
    ScheduleMode,
    ScheduleSettings,
    SubmissionState,
    TaskPhase,
    UTC,
)
from topps_auto_bot.runner import FullFlowRunner, SystemClock
from topps_auto_bot.state import RunStateStore


def make_full_runner(
    *,
    adapter,
    policy,
    site,
    tmp_path: Path,
    preset=None,
    order_limit: str = "70.00",
    events=None,
    cancel_event=None,
    manual_login_timeout: float = 600.0,
):
    preset = preset or make_preset(url=f"{site.base}/flow-product.html")
    config = make_config(presets=(preset,), selected=preset.preset_id, order_limit=order_limit)
    snapshot = make_snapshot(
        config=config,
        schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60),
    )
    store = RunStateStore(tmp_path / "run-state.json")
    runner = FullFlowRunner(
        snapshot=snapshot,
        session=adapter,
        store=store,
        clock=SystemClock(),
        events=events or queue.Queue(),
        cancel_event=cancel_event or threading.Event(),
        policy=policy,
        preparation_timeout_seconds=20.0,
        result_timeout_seconds=30.0,
        poll_interval_seconds=0.05,
        result_poll_interval_seconds=0.1,
        manual_login_timeout_seconds=manual_login_timeout,
    )
    return runner, store


def test_local_full_flow_empty_cart_to_order(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    runner, store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    counts = {"add": 0, "submit": 0}
    original_add = adapter.add_to_cart
    original_submit = adapter.submit_purchase

    def counted_add():
        counts["add"] += 1
        return original_add()

    def counted_submit():
        counts["submit"] += 1
        return original_submit()

    adapter.add_to_cart = counted_add  # type: ignore[assignment]
    adapter.submit_purchase = counted_submit  # type: ignore[assignment]
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert counts == {"add": 1, "submit": 1}
    assert site.login_submissions == 0
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "REUSED"
    assert len(site.cart_items) == 1
    assert site.cart_items[0]["variant"] == "Hobby Box"
    assert site.cart_items[0]["quantity"] == 2
    loaded = store.load()
    assert loaded.add_to_cart is not None
    assert loaded.add_to_cart.state is AddToCartState.CONFIRMED
    assert loaded.purchase is not None
    assert loaded.purchase.state is SubmissionState.SUCCEEDED
    assert not loaded.blocking
    previous = None
    for event in outcome.events:
        delta = 0.0 if previous is None else (event.at_utc - previous).total_seconds()
        print(f"STAGE {event.at_utc.isoformat()} +{delta:.3f}s {event.phase}")
        previous = event.at_utc


def test_local_full_flow_existing_cart_stops(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    site.add_item("Synthetic Card", "Hobby Box", 1)
    runner, store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    counts = {"add": 0}
    original_add = adapter.add_to_cart

    def counted_add():
        counts["add"] += 1
        return original_add()

    adapter.add_to_cart = counted_add  # type: ignore[assignment]
    outcome = runner.run()
    assert outcome.phase is TaskPhase.STOPPED
    assert counts["add"] == 0
    assert len(site.cart_items) == 1
    assert store.load().status.name == "MISSING"


def test_local_full_flow_sets_quantity_and_variant(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    preset = make_preset(
        url=f"{site.base}/flow-product.html",
        variant="Retail Box",
        quantity=3,
    )
    runner, _store = make_full_runner(
        adapter=adapter,
        policy=policy,
        site=site,
        tmp_path=tmp_path,
        preset=preset,
        order_limit="100.00",
    )
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert site.cart_items[0]["variant"] == "Retail Box"
    assert site.cart_items[0]["quantity"] == 3


def test_local_full_flow_over_price_stops_before_add(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    preset = make_preset(url=f"{site.base}/flow-product.html", unit_price_limit="19.99")
    runner, _store = make_full_runner(
        adapter=adapter,
        policy=policy,
        site=site,
        tmp_path=tmp_path,
        preset=preset,
    )
    counts = {"add": 0}
    original_add = adapter.add_to_cart

    def counted_add():
        counts["add"] += 1
        return original_add()

    adapter.add_to_cart = counted_add  # type: ignore[assignment]
    outcome = runner.run()
    assert outcome.phase is TaskPhase.STOPPED
    assert counts["add"] == 0
    assert site.cart_items == []



def test_local_auto_login_then_full_flow(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    site.logout()
    runner, _store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert site.login_submissions == 1
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "AUTO"
    assert site.cart_items[0]["quantity"] == 2
    print(
        "LOGIN",
        outcome.login_preparation.mode,
        f"{outcome.login_preparation.elapsed_seconds:.3f}s",
    )
    previous = None
    for event in outcome.events:
        delta = 0.0 if previous is None else (event.at_utc - previous).total_seconds()
        print(f"STAGE {event.at_utc.isoformat()} +{delta:.3f}s {event.phase}")
        previous = event.at_utc


def _run_until_manual_login(*, runner, site, timeout: float = 10.0):
    result: dict = {}

    def target() -> None:
        result["outcome"] = runner.run()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    deadline = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < deadline:
        try:
            event = runner.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event.phase is TaskPhase.AWAITING_MANUAL_LOGIN:
            return thread, result
    thread.join(timeout=1)
    raise AssertionError("未进入人工登录等待")


def test_local_auto_login_failure_then_manual_success(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    site.logout()
    site.expected_password = "wrong-password"
    runner, _store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    thread, result = _run_until_manual_login(runner=runner, site=site)
    assert site.login_submissions == 1
    assert site.cart_items == []
    site.complete_manual_login()
    thread.join(timeout=60)
    assert not thread.is_alive()
    outcome = result["outcome"]
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert site.login_submissions == 1
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "MANUAL"
    assert len(site.cart_items) == 1


def test_local_login_challenge_requires_manual_without_retry(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    site.logout()
    site.challenge_next_login = True
    runner, _store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    thread, result = _run_until_manual_login(runner=runner, site=site)
    assert site.login_submissions == 1
    site.complete_manual_login()
    thread.join(timeout=60)
    assert not thread.is_alive()
    outcome = result["outcome"]
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert site.login_submissions == 1
    assert outcome.login_preparation is not None
    assert outcome.login_preparation.mode == "MANUAL"
