from __future__ import annotations

import queue
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from tests.support import make_config, make_preset, make_snapshot
from topps_auto_bot.browser import TrustedSourcePolicy
from topps_auto_bot.models import (
    AddToCartState,
    LoginCredentials,
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
    auto_login: bool = False,
):
    adapter.navigate(site.base + ("/login.html" if site.logged_in_email is None else "/"))
    preset = preset or make_preset(url=f"{site.base}/flow-product.html")
    config = make_config(presets=(preset,), selected=preset.preset_id, order_limit=order_limit)
    snapshot = make_snapshot(
        config=config,
        schedule=ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60),
    )
    snapshot = replace(snapshot, auto_login=auto_login, login=snapshot.login if auto_login else LoginCredentials(""))
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

    def counted_add(**kwargs):
        counts["add"] += 1
        return original_add(**kwargs)

    def counted_submit(**kwargs):
        counts["submit"] += 1
        return original_submit(**kwargs)

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

    def counted_add(**kwargs):
        counts["add"] += 1
        return original_add(**kwargs)

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

    def counted_add(**kwargs):
        counts["add"] += 1
        return original_add(**kwargs)

    adapter.add_to_cart = counted_add  # type: ignore[assignment]
    outcome = runner.run()
    assert outcome.phase is TaskPhase.STOPPED
    assert counts["add"] == 0
    assert site.cart_items == []



def test_local_auto_login_then_full_flow(browser, local_site, tmp_path: Path) -> None:
    adapter, policy, site = browser
    site.reset()
    site.logout()
    runner, _store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path, auto_login=True)
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
    runner, _store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path, auto_login=True)
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
    runner, _store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path, auto_login=True)
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


def test_read_link_select_variant_and_run_temporary_target(browser, tmp_path, monkeypatch):
    import gc
    from tkinter import messagebox

    import pytest

    from tests.unit.test_ui_smoke import new_root
    from topps_auto_bot import settings
    from topps_auto_bot.settings import AppPaths, save_config
    from topps_auto_bot.ui import ToppsAutoBotApp

    adapter, policy, site = browser
    site.reset()
    url = site.base + "/flow-product.html"
    original_validate_url = settings.validate_product_url
    monkeypatch.setattr(
        settings, "validate_product_url",
        lambda value: value if value == url else original_validate_url(value),
    )
    root = new_root()
    paths = AppPaths.under(tmp_path / "ui-data")
    paths.ensure_directories()
    save_config(paths.config, make_config())
    monkeypatch.setattr(messagebox, "showerror", lambda title, message: pytest.fail(f"{title}: {message}"))
    monkeypatch.setattr(ToppsAutoBotApp, "_open_browser", lambda self: True)
    app = ToppsAutoBotApp(root, paths)
    try:
        app.session = adapter
        app.manager.policy = policy
        app.preset_url_var.set(url)
        app._read_product_link()
        assert app.preset_title_var.get() == "Synthetic Card"
        assert app.preset_variant_var.get() == ""
        assert "Hobby Box" in app.status_var.get() and "Retail Box" in app.status_var.get()
        app.preset_variant_var.set("Retail Box")
        app.preset_quantity_var.set("2")
        app.preset_price_var.set("25.00")
        app._use_editor_target()
        app._start_task()
        assert app.manager.join(timeout=25)
        assert app.store.load().purchase.state is SubmissionState.SUCCEEDED
        assert [(item["variant"], item["quantity"]) for item in site.cart_items] == [("Retail Box", 2)]
    finally:
        app.manager.stop()
        app.manager.join(timeout=35)
        root.destroy()
        # Finalize Tk variables here, before a browser fixture thread can collect them.
        del app, root
        gc.collect()


def test_browser_temporary_alert_with_empty_cart_blocks_new_attempt(browser, tmp_path, monkeypatch):
    from tests.support import FakeClock

    adapter, policy, site = browser
    site.reset()
    original_html = site.html
    add_requests = 0

    def rejected_page(path):
        nonlocal add_requests
        if path.startswith("/rejected-add?"):
            add_requests += 1
            return original_html("/flow-product.html").replace(
                b'<button data-action="add-to-cart"',
                b'<p role="alert" data-add-error>Temporarily unavailable</p><button data-action="add-to-cart"',
            )
        return original_html(path).replace(b'action="/add"', b'action="/rejected-add"')

    monkeypatch.setattr(site, "html", rejected_page)
    runner, store = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    runner.clock = FakeClock()
    runner.poll_interval_seconds = 5
    original_add = adapter.add_to_cart
    observed = []

    def observe_alert_result(**kwargs):
        result = original_add(**kwargs)
        observed.append((result, adapter.read_product_page().add_to_cart_enabled))
        return result

    monkeypatch.setattr(adapter, "add_to_cart", observe_alert_result)
    outcome = runner.run()
    [(result, button_enabled)] = observed
    assert result.attempted and result.uncertain and not result.confirmed_no_side_effect
    assert button_enabled
    assert outcome.phase is TaskPhase.UNKNOWN
    assert add_requests == 1 and site.cart_items == []
    saved = RunStateStore(store.path).load()
    assert saved.blocking and saved.add_to_cart.state is AddToCartState.UNKNOWN
    assert saved.purchase is None
    next_runner, _ = make_full_runner(adapter=adapter, policy=policy, site=site, tmp_path=tmp_path)
    assert next_runner.run().phase is TaskPhase.BLOCKED
    assert add_requests == 1
