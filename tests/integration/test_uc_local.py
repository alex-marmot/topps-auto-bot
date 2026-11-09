from __future__ import annotations

import queue
import shutil
import threading
from dataclasses import replace
from pathlib import Path

from tests.support import make_config, make_preset, make_snapshot
from topps_auto_bot.browser import (
    DEFAULT_TOPPs_PROFILE,
    SeleniumPageAdapter,
    TrustedSourcePolicy,
    open_live_session,
)
from topps_auto_bot.driver_setup import inspect_chrome, patch_driver_for_uc
from topps_auto_bot.models import ScheduleMode, ScheduleSettings, SubmissionState, TaskPhase
from topps_auto_bot.runner import FullFlowRunner, SystemClock
from topps_auto_bot.state import AddToCartState, RunStateStore


def test_uc_local_full_flow_uses_managed_driver_without_no_sandbox(
    chromedriver_path: Path,
    chrome_path: Path,
    local_site,
    tmp_path: Path,
) -> None:
    site = local_site
    site.reset()
    driver_copy = tmp_path / "chromedriver"
    shutil.copy2(chromedriver_path, driver_copy)
    driver_copy.chmod(0o755)
    chrome = inspect_chrome(chrome_path)
    patch_driver_for_uc(driver_copy, chrome_major=chrome.major_version)
    origin = site.base
    policy = TrustedSourcePolicy(
        allowed_payment_origins=frozenset({origin}),
        allow_loopback_http=True,
    )
    profile = replace(
        DEFAULT_TOPPs_PROFILE,
        name="uc-local-test",
        verified=True,
        search_url_template=origin + "/search?q={query}",
        cart_url=origin + "/flow-cart.html",
        login_url=origin + "/login.html",
    )
    adapter = open_live_session(
        chrome_path=str(chrome_path),
        driver_path=str(driver_copy),
        profile_dir=tmp_path / "uc-profile",
        policy=policy,
        profile=profile,
        page_load_timeout=30.0,
        headless=True,
        use_uc=True,
        no_sandbox=False,
    )
    try:
        assert "--no-sandbox" not in adapter._driver.options.arguments
        preset = make_preset(url=f"{site.base}/flow-product.html")
        config = make_config(presets=(preset,), selected=preset.preset_id, order_limit="70.00")
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
        loaded = store.load()
        assert loaded.add_to_cart is not None
        assert loaded.add_to_cart.state is AddToCartState.CONFIRMED
        assert loaded.purchase is not None
        assert loaded.purchase.state is SubmissionState.SUCCEEDED
    finally:
        adapter.close()
