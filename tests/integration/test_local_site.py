from __future__ import annotations

import queue
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support import make_config, make_snapshot
from topps_auto_bot.browser import TrustedSourcePolicy
from topps_auto_bot.models import (
    PreparedLevel,
    ScheduleMode,
    ScheduleSettings,
    SubmissionState,
    TaskPhase,
    UTC,
)
from topps_auto_bot.purchase import SubmissionStore, build_submission_record
from topps_auto_bot.runner import PerformanceSample, PurchaseRunner, SystemClock, performance_summary


def make_runner(
    *,
    browser,
    policy,
    local_site,
    tmp_path: Path,
    window_seconds: int = 60,
    snapshot=None,
    result_timeout: float = 60.0,
):
    snapshot = snapshot or make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=window_seconds),
    )
    store = SubmissionStore(tmp_path / "run-state.json")
    runner = PurchaseRunner(
        snapshot=snapshot,
        session=browser,
        store=store,
        clock=SystemClock(),
        events=queue.Queue(),
        cancel_event=threading.Event(),
        policy=policy,
        preparation_timeout_seconds=20.0,
        result_timeout_seconds=result_timeout,
        poll_interval_seconds=0.1,
        result_poll_interval_seconds=0.2,
    )
    return runner, store


def test_prepared_cart_end_to_end(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/cart.html")
    runner, store = make_runner(browser=adapter, policy=policy, local_site=site, tmp_path=tmp_path)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert store.load().record is not None
    assert store.load().record.state is SubmissionState.SUCCEEDED
    assert adapter.current_url().endswith("/confirmation.html")
    assert any(sample.action == "enter_checkout" for sample in outcome.performance)
    assert any(sample.action == "submit_purchase" for sample in outcome.performance)


def test_prepared_checkout_end_to_end(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/checkout.html")
    runner, store = make_runner(browser=adapter, policy=policy, local_site=site, tmp_path=tmp_path)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert store.load().record.prepared_level is PreparedLevel.CHECKOUT_READY
    assert adapter.current_url().endswith("/confirmation.html")


def test_empty_checkout_form_is_filled_before_submit(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/checkout-empty-form.html")
    runner, _store = make_runner(browser=adapter, policy=policy, local_site=site, tmp_path=tmp_path)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert adapter.current_url().endswith("/confirmation.html")


def test_delayed_checkout_entry_is_observed(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/cart-delayed.html")
    runner, _store = make_runner(browser=adapter, policy=policy, local_site=site, tmp_path=tmp_path)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert adapter.current_url().endswith("/confirmation.html")


def test_window_expiry_does_not_submit(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/checkout-disabled.html")
    runner, store = make_runner(
        browser=adapter,
        policy=policy,
        local_site=site,
        tmp_path=tmp_path,
        window_seconds=1,
    )
    outcome = runner.run()
    assert outcome.phase is TaskPhase.EXPIRED
    assert adapter.current_url().endswith("/checkout-disabled.html")
    assert store.load().record is None


def test_link_reading_and_search_do_not_modify_cart(browser, local_site) -> None:
    adapter, _policy, site = browser
    adapter.navigate(f"{site.base}/cart.html")
    before = adapter.read_cart()
    product = adapter.read_product(f"{site.base}/product.html")
    assert product.title == "Synthetic Card"
    assert product.variants == ("Hobby Box", "Retail Box")
    search = adapter.search_products("synthetic")
    assert len(search.candidates) == 2
    assert all(candidate.url.startswith(site.base) for candidate in search.candidates)
    adapter.navigate(f"{site.base}/cart.html")
    after = adapter.read_cart()
    assert after.lines == before.lines


def test_scheduled_local_site_trigger_deviation(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/checkout.html")
    from datetime import datetime, timedelta, timezone

    # Leave time for the real browser to fill and verify the form before the scheduled start.
    planned = datetime.now(timezone.utc) + timedelta(seconds=5)
    snapshot = make_snapshot(
        config=make_config(),
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=planned, window_seconds=30),
    )
    runner, _store = make_runner(
        browser=adapter,
        policy=policy,
        local_site=site,
        tmp_path=tmp_path,
        snapshot=snapshot,
    )
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    assert outcome.window is not None
    deviation = (outcome.window.started_at_utc - planned).total_seconds()
    print("scheduled trigger deviation seconds", deviation)
    assert -0.05 <= deviation < 1.0
    assert outcome.window.effective_start_utc == planned


def test_payment_origin_not_in_allow_list_stops(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    adapter.navigate(f"{site.base}/checkout.html")
    runner, _store = make_runner(
        browser=adapter,
        policy=TrustedSourcePolicy(allow_loopback_http=True),
        local_site=site,
        tmp_path=tmp_path,
    )
    outcome = runner.run()
    assert outcome.phase is TaskPhase.FAILED
    assert adapter.current_url().endswith("/checkout.html")


def _run_iteration(adapter, policy, site, url: str, tmp_path: Path, index: int):
    adapter.navigate(url)
    snapshot = make_snapshot(
        config=make_config(),
        task_id=f"perf-{index}",
        schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=120),
    )
    store = SubmissionStore(tmp_path / f"run-state-{index}.json")
    runner = PurchaseRunner(
        snapshot=snapshot,
        session=adapter,
        store=store,
        clock=SystemClock(),
        events=queue.Queue(),
        cancel_event=threading.Event(),
        policy=policy,
        preparation_timeout_seconds=20.0,
        result_timeout_seconds=30.0,
        poll_interval_seconds=0.05,
        result_poll_interval_seconds=0.1,
    )
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED
    return outcome.performance, outcome.window


@pytest.mark.legacy_performance
def test_both_paths_30_warm_runs_p95_under_500ms(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    samples: list[PerformanceSample] = []
    cart_windows = []
    checkout_windows = []
    for index in range(30):
        run_samples, run_window = _run_iteration(adapter, policy, site, f"{site.base}/cart.html", tmp_path, index)
        samples.extend(run_samples)
        cart_windows.append(run_window)
    for index in range(30):
        run_samples, run_window = _run_iteration(
            adapter, policy, site, f"{site.base}/checkout.html", tmp_path, 100 + index
        )
        samples.extend(run_samples)
        checkout_windows.append(run_window)
    cart = performance_summary(samples, PreparedLevel.CART_READY, "enter_checkout")
    checkout = performance_summary(samples, PreparedLevel.CHECKOUT_READY, "submit_purchase")
    print("cart path client performance", cart)
    print("checkout path client performance", checkout)

    def _seconds(value) -> float:
        return value.total_seconds() * 1000.0

    cart_samples = [s for s in samples if s.path is PreparedLevel.CART_READY and s.action == "enter_checkout"]
    checkout_samples = [
        s for s in samples if s.path is PreparedLevel.CHECKOUT_READY and s.action == "submit_purchase"
    ]
    for label, window_list, path_samples in (
        ("cart", cart_windows, cart_samples),
        ("checkout", checkout_windows, checkout_samples),
    ):
        discovery = [
            _seconds(sample.observed_at_utc - window.effective_start_utc)
            for window, sample in zip(window_list, path_samples)
            if window is not None
        ]
        full = [
            _seconds(sample.action_at_utc - window.effective_start_utc)
            for window, sample in zip(window_list, path_samples)
            if window is not None
        ]
        print(
            f"{label} path discovery delay ms:",
            performance_summary([PerformanceSample(PreparedLevel.CART_READY, "d", s.observed_at_utc, s.observed_at_utc, d) for s, d in zip(path_samples, discovery)]),
        )
        print(
            f"{label} path full-path ms:",
            performance_summary([PerformanceSample(PreparedLevel.CART_READY, "f", s.observed_at_utc, s.action_at_utc, d) for s, d in zip(path_samples, full)]),
        )
    cart_submit_samples = [
        s for s in samples if s.path is PreparedLevel.CART_READY and s.action == "submit_purchase"
    ]
    cart_full_to_submit = [
        _seconds(sample.action_at_utc - window.effective_start_utc)
        for window, sample in zip(cart_windows, cart_submit_samples)
        if window is not None
    ]
    print(
        "cart path full-to-submit ms:",
        performance_summary(
            [
                PerformanceSample(PreparedLevel.CART_READY, "f", s.observed_at_utc, s.action_at_utc, d)
                for s, d in zip(cart_submit_samples, cart_full_to_submit)
            ]
        ),
    )
    assert cart["count"] == 30
    assert checkout["count"] == 30
    assert cart["p95_ms"] is not None and cart["p95_ms"] <= 500.0
    assert checkout["p95_ms"] is not None and checkout["p95_ms"] <= 500.0
