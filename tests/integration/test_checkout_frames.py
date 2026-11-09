from __future__ import annotations

import html
import json
import queue
import shutil
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from selenium.webdriver.support.ui import WebDriverWait

from tests.support import FakeClock, make_config, make_preset, make_snapshot
from topps_auto_bot import browser
from topps_auto_bot.browser import DEFAULT_TOPPs_PROFILE, TrustedSourcePolicy, open_live_session
from topps_auto_bot.models import LoginCredentials, OrderStatus, SubmissionState, TaskPhase, parse_utc_iso
from topps_auto_bot.runner import FullFlowRunner, SystemClock
from topps_auto_bot.state import RunStateStore


@contextmanager
def serve(render):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = render(self.path)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", body)
                self.end_headers()
                return
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<!doctype html>" + body).encode())

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            worker.join()


@pytest.fixture
def storefront():
    class Site:
        mode = "bank"
        logged_in = True
        clicks = 0
        quantity = 0

        def render(self, path):
            route = urlsplit(path).path
            if route == "/add":
                self.quantity = int(parse_qs(urlsplit(path).query)["quantity"][0])
                return 302, "/checkout/cart"
            if route == "/manual":
                self.logged_in = True
                return 302, "/"
            if route == "/pay":
                self.clicks += 1
                return 302, "/bank" if self.mode == "bank" else "/order"
            account = '<a href="/customer/account/logout">Sign out</a>' if self.logged_in else (
                '<a href="/customer/account/login">Sign in</a>'
            )
            line = f'''
              <span class="product__description__name">Synthetic Card</span>
              <span class="product__description__variant">Hobby Box</span>
              <span class="product__quantity">{self.quantity}</span>
              <span aria-label="Unit price">$20.00</span>'''
            quote = '''
              <span aria-label="Subtotal">$40.00</span>
              <span aria-label="Estimated taxes">$5.00</span>
              <span aria-label="Shipping total">$10.00</span>
              <span aria-label="Total">USD $55.00</span>
              <label><input type="radio" name="shipping-rate" value="standard" checked>
                <span aria-label="Shipping service">Standard</span>
                <span aria-label="Shipping price">$10.00</span>
              </label>'''
            if route == "/":
                if "?q=" in path:
                    entries = [
                        '<a href="/products/synthetic"><h3>Synthetic Card</h3></a>',
                        '<a href="/products/similar"><h3>Synthetic Card Variant</h3></a>',
                    ]
                    if "reverse" in path:
                        entries.reverse()
                    results = '<p role="status">No products found</p>' if "none" in path else (
                        '<label>In Stock Only <input type="checkbox" name="in-stock"></label>' + "".join(entries)
                    )
                    return 200, account + f'<main id="search-results">{results}</main>'
                return 200, account + '<header><form action="/" method="get"><input type="search" name="q"></form></header>'
            if route == "/customer/account/login":
                return 200, '<main><input autocomplete="one-time-code"><a href="/manual">Complete login</a></main>'
            if route == "/products/synthetic":
                return 200, account + '''
                  <main><h1>Synthetic Card</h1>
                    <form action="/add"><select name="variant"><option>Hobby Box</option><option>Retail Box</option></select>
                      <input name="quantity" type="number" value="1" min="1"><span itemprop="price">$20.00</span>
                      <button name="add">Add to cart</button>
                    </form>
                  </main>'''
            if route == "/checkout/cart":
                contents = "<h1>Your cart's empty</h1>" if self.quantity == 0 else (
                    f'<ul><li data-testid="cart-item">{line}</li></ul><a href="/checkouts/session">Checkout</a>'
                )
                return 200, account + f"<main>{contents}</main>"
            if route == "/order":
                return 200, f'<main><span id="confirmation-number">SYNTHETIC-ORDER</span><span class="payment-status">paid</span><table class="product-table"><tbody><tr><td>{line}</td></tr></tbody></table></main>'
            if route == "/bank":
                return 200, f'<iframe title="Bank verification" src="{self.bank}/challenge"></iframe>'
            if route == "/verification-cancelled":
                return 200, '<main>Verification cancelled. Order status is unavailable.</main>'
            if route != "/checkouts/session":
                return 404, "<main>Unknown page</main>"
            contact = '''
              <input autocomplete="given-name" required><input autocomplete="family-name" required>
              <input id="email" type="email" required><input autocomplete="tel" required>
              <input autocomplete="address-line1" required><input autocomplete="address-level2" required>
              <select autocomplete="address-level1" required><option value="">Select</option><option value="TS">Test State</option></select>
              <input autocomplete="postal-code" required>
              <select autocomplete="country" required><option value="">Select</option><option value="US">United States</option></select>
              '''
            frames = []
            for field, origin in self.frames.items():
                if self.mode == "missing-frame" and field == "verification_value":
                    continue
                src = origin + ("/redirect" if self.mode == "redirect" and field == "expiry" else "/frame")
                if self.mode == "missing-field" and field == "expiry":
                    src = origin + "/missing"
                frames.append(f'<iframe id="card-fields-{field}" src="{src}"></iframe>')
            scripts = """
              const pay = document.querySelector('#checkout-pay-button');
              const valid = new Set();
              window.addEventListener('message', event => {
                const frame = document.querySelector('#card-fields-' + event.data.field);
                if (!frame || event.source !== frame.contentWindow || event.origin !== new URL(frame.src).origin) return;
                if (event.data.valid) valid.add(event.data.field);
                else valid.delete(event.data.field);
                pay.disabled = valid.size !== 3;
              });
            """
            if self.mode in {"rebuild", "rebuild-timeout", "replace-checkout"}:
                target = "document.querySelector('main')" if self.mode == "replace-checkout" else "document.querySelector('#card-fields-number')"
                replacement_src = "copy.src = new URL('/rebuilt', node.src).href;" if self.mode == "rebuild-timeout" else ""
                scripts += f'''
                    window.addEventListener('message', event => {{
                      if (event.data.field !== 'number' || !event.data.valid || window.rebuilt) return;
                      window.rebuilt = true;
                      const node = {target};
                      const copy = node.cloneNode(true);
                      {replacement_src}
                      valid.delete('number');
                      node.replaceWith(copy);
                    }});
                '''
            if self.mode == "delay":
                delayed = frames.pop()
                scripts += "setTimeout(() => payment.insertAdjacentHTML('beforeend', " + json.dumps(delayed) + "), 1000);"
            trap = '<input autocomplete="cc-csc" value="parent-trap">' if self.mode == "missing-frame" else ""
            if self.mode == "rebuild-timeout":
                trap = '<input autocomplete="cc-number" value="parent-trap">'
            return 200, account + f'''
              <main id="checkout-main"><section aria-label="Order summary"><table class="product-table">
                <tbody><tr><td>{line}</td></tr></tbody></table>{quote}</section>
                {contact}<section id="payment"><input autocomplete="cc-name" required>{"".join(frames)}{trap}</section>
                <button id="checkout-pay-button" disabled onclick="location.href='/pay'">Pay now</button>
              </main><script>{scripts}</script>'''

    site = Site()
    with ExitStack() as stack:
        site.base = stack.enter_context(serve(site.render))
        site.bank = stack.enter_context(serve(lambda _: (
            200, f'<a target="_top" href="{site.base}/order">Verify purchase</a>'
                 f'<a target="_top" href="{site.base}/verification-cancelled">Cancel verification</a>'
                 + ('<input autocomplete="cc-exp">' if site.mode == "redirect" else ''),
        )))
        site.frames = {}
        for field, autocomplete, pattern in (
            ("number", "cc-number", "[0-9]{16}"),
            ("expiry", "cc-exp", "[0-9]{2}/[0-9]{4}"),
            ("verification_value", "cc-csc", "[0-9]{3}"),
        ):
            def render(path, field=field, autocomplete=autocomplete, pattern=pattern):
                if path == "/redirect":
                    return 302, site.bank + "/challenge"
                if path == "/missing":
                    return 200, '<p role="alert">Payment input unavailable</p>'
                script = f'''
                    <script>const el = document.querySelector('input');
                    el.addEventListener('input', () => parent.postMessage(
                      {{field: '{field}', valid: el.validity.valid}}, '{site.base}'));
                    </script>'''
                # Exercise the native setter fallback on a real controlled input.
                if site.mode == "js-fallback" and field == "verification_value":
                    script += "<script>el.addEventListener('keydown', e => e.preventDefault());</script>"
                disabled = "disabled" if path == "/rebuilt" else ""
                return 200, f'<input id="{field}" autocomplete="{autocomplete}" required pattern="{html.escape(pattern)}" {disabled}>' + script

            site.frames[field] = stack.enter_context(serve(render))
        yield site


@pytest.fixture
def representative_browser(chromedriver_path, chrome_path, tmp_path, storefront):
    policy = TrustedSourcePolicy(
        allowed_payment_origins=frozenset({storefront.base, *storefront.frames.values()}),
        allow_loopback_http=True,
    )
    profile = replace(DEFAULT_TOPPs_PROFILE, home_url=storefront.base + "/", cart_url=storefront.base + "/checkout/cart")
    adapter = open_live_session(
        chrome_path=str(chrome_path), driver_path=str(chromedriver_path),
        profile_dir=tmp_path / "isolated-profile", policy=policy, profile=profile,
        headless=True, use_uc=False, page_load_timeout=5.0,
    )
    try:
        yield adapter, policy
    finally:
        adapter.close()


def run_from_product(adapter, policy, site, tmp_path, *, clock=None):
    preset = make_preset(url=site.base + "/products/synthetic")
    config = make_config(presets=(preset,), selected=preset.preset_id, login=LoginCredentials(""))
    snapshot = make_snapshot(config=config)
    store = RunStateStore(tmp_path / "run-state.json")
    runner = FullFlowRunner(
        snapshot=snapshot, session=adapter, store=store, clock=clock or SystemClock(),
        events=queue.Queue(), cancel_event=threading.Event(), policy=policy,
        poll_interval_seconds=0.05, result_poll_interval_seconds=1.0 if clock else 0.1,
        result_timeout_seconds=8.0,
    )
    return runner, store


@pytest.mark.parametrize("mode", ["bank", "delay", "rebuild", "js-fallback"])
def test_actual_frames_from_empty_cart_to_single_payment(representative_browser, storefront, tmp_path, mode):
    adapter, policy = representative_browser
    storefront.mode = mode
    adapter.navigate(storefront.base + "/")
    runner, store = run_from_product(adapter, policy, storefront, tmp_path)
    if mode == "bank":
        original = adapter.read_order_result

        def bank_step():
            result = original()
            if result.status is OrderStatus.VERIFICATION:
                driver = adapter._driver
                try:
                    driver.switch_to.frame(driver.find_element("css selector", "iframe"))
                    driver.find_element("link text", "Verify purchase").click()
                finally:
                    driver.switch_to.default_content()
            return result

        adapter.read_order_result = bank_step
    outcome = runner.run()
    assert outcome.phase is TaskPhase.SUCCEEDED, outcome.message
    assert storefront.quantity == 2
    assert storefront.clicks == 1
    assert store.load().purchase.state is SubmissionState.SUCCEEDED
    assert adapter._driver.execute_script("return window === window.top")
    if mode == "bank":
        assert any("银行验证" in event.message for event in outcome.events)


@pytest.mark.parametrize(("mode", "reason"), [
    ("missing-frame", "缺少信用卡 iframe"),
    ("missing-field", "缺少结算字段"),
    ("redirect", "实际来源不可信"),
    ("replace-checkout", "结算页面已变化"),
])
def test_invalid_frames_never_submit(representative_browser, storefront, tmp_path, mode, reason):
    adapter, policy = representative_browser
    storefront.mode = mode
    adapter.navigate(storefront.base + "/")
    runner, store = run_from_product(adapter, policy, storefront, tmp_path)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.FAILED, outcome.message
    assert reason in outcome.message
    assert storefront.clicks == 0
    assert store.load().purchase is None
    assert adapter._driver.execute_script("return window === window.top")
    if mode == "redirect":
        driver = adapter._driver
        driver.switch_to.frame(driver.find_element("id", "card-fields-expiry"))
        try:
            assert driver.find_element("css selector", '[autocomplete="cc-exp"]').get_property("value") == ""
        finally:
            driver.switch_to.default_content()
    if mode == "missing-frame":
        assert adapter._driver.find_element("css selector", 'input[autocomplete="cc-csc"]').get_property("value") == "parent-trap"


def test_rebuilt_frame_does_not_reset_fill_deadline(representative_browser, storefront, tmp_path, monkeypatch):
    adapter, policy = representative_browser
    storefront.mode = "rebuild-timeout"
    adapter.navigate(storefront.base + "/")
    runner, store = run_from_product(adapter, policy, storefront, tmp_path)
    driver = adapter._driver
    original_fill = adapter.fill_checkout
    original_deadline = adapter._page_load_timeout
    elapsed = iter([0.0])
    rebuilt = False

    def monotonic():
        return next(elapsed, original_deadline if rebuilt else original_deadline - 1)

    def make_replacement_editable(_seconds):
        nonlocal rebuilt
        assert not rebuilt, "rebuild should not restart the original timeout"
        assert driver.execute_script("return window === window.top")
        WebDriverWait(driver, 3).until(
            lambda page: page.execute_script("return window.rebuilt === true")
        )
        driver.switch_to.frame(driver.find_element("id", "card-fields-number"))
        try:
            field = WebDriverWait(driver, 3).until(
                lambda frame: frame.find_element("css selector", "input[disabled]")
            )
            assert driver.execute_script("return window.location.href") == storefront.frames["number"] + "/rebuilt"
            driver.execute_script("arguments[0].disabled = false;", field)
        finally:
            driver.switch_to.default_content()
        rebuilt = True

    def fill_with_clock(*args, **kwargs):
        # Keep real DOM, origin checks and frame switches. Advance the adapter clock to
        # the original deadline as the new frame becomes editable during the first retry.
        with monkeypatch.context() as scoped:
            scoped.setattr(browser, "time", SimpleNamespace(
                monotonic=monotonic, sleep=make_replacement_editable,
            ))
            return original_fill(*args, **kwargs)

    monkeypatch.setattr(adapter, "fill_checkout", fill_with_clock)
    outcome = runner.run()
    assert rebuilt
    assert outcome.phase is TaskPhase.FAILED, outcome.message
    assert outcome.message.startswith("结算资料填写失败：")
    # Chrome can observe the replacement while the old element is in use, while
    # the new frame is loading, or when it checks the new field's state.
    assert outcome.message.split("：", 1)[1] in {
        "信用卡 iframe 或字段重建后未能及时核实",
        "信用卡 iframe 正在加载",
        "缺少结算字段：卡号",
        "结算字段暂不可编辑：卡号",
        "结算字段值或校验状态已变化：卡号",
    }
    assert driver.execute_script("return window === window.top")
    assert driver.find_element("css selector", 'input[autocomplete="cc-number"]').get_property("value") == "parent-trap"
    driver.switch_to.frame(driver.find_element("id", "card-fields-number"))
    try:
        field = driver.find_element("css selector", 'input[autocomplete="cc-number"]')
        assert field.is_enabled()
        assert field.get_property("value") == ""
    finally:
        driver.switch_to.default_content()
    assert storefront.clicks == 0
    assert store.load().purchase is None


@pytest.mark.parametrize("cancel_challenge", [True, False], ids=["cancel", "timeout"])
def test_bank_verification_leaves_unconfirmed_payment_blocking(
    representative_browser, storefront, tmp_path, monkeypatch, cancel_challenge,
):
    adapter, policy = representative_browser
    adapter.navigate(storefront.base + "/")
    clock = FakeClock()
    runner, store = run_from_product(adapter, policy, storefront, tmp_path, clock=clock)
    driver = adapter._driver
    original_read = adapter.read_order_result
    observed = []

    def observe_bank():
        result = original_read()
        if not observed:
            assert result.status is OrderStatus.VERIFICATION
            driver.switch_to.frame(driver.find_element("css selector", 'iframe[title="Bank verification"]'))
            try:
                assert driver.execute_script("return window.location.href") == storefront.bank + "/challenge"
                if cancel_challenge:
                    driver.find_element("link text", "Cancel verification").click()
            finally:
                driver.switch_to.default_content()
        observed.append(result.status)
        return result

    monkeypatch.setattr(adapter, "read_order_result", observe_bank)
    outcome = runner.run()
    assert outcome.phase is TaskPhase.UNKNOWN, outcome.message
    assert observed[0] is OrderStatus.VERIFICATION
    assert observed[-1] is (OrderStatus.UNKNOWN if cancel_challenge else OrderStatus.VERIFICATION)
    assert any("银行验证" in event.message for event in outcome.events)
    assert driver.execute_script("return window === window.top")
    path = "/verification-cancelled" if cancel_challenge else "/bank"
    assert adapter.current_url() == storefront.base + path

    reloaded = RunStateStore(store.path).load()
    assert reloaded.blocking
    assert reloaded.purchase.state is SubmissionState.UNKNOWN
    assert clock.now() == parse_utc_iso(reloaded.purchase.submitted_at_utc) + timedelta(
        seconds=runner.result_timeout_seconds
    )
    new_runner, _ = run_from_product(adapter, policy, storefront, tmp_path, clock=clock)
    assert new_runner.run().phase is TaskPhase.BLOCKED
    assert storefront.clicks == 1


def test_home_search_and_passwordless_manual_login(representative_browser, storefront, tmp_path):
    adapter, policy = representative_browser
    storefront.mode = "delay"
    result = adapter.search_products("synthetic")
    assert [candidate.title for candidate in result.candidates] == ["Synthetic Card", "Synthetic Card Variant"]
    assert adapter._driver.find_element("name", "in-stock").is_selected()
    reordered = adapter.search_products("synthetic reverse")
    assert [item.url for item in reordered.candidates] == [item.url for item in reversed(result.candidates)]
    assert not adapter.search_products("none").candidates
    assert storefront.quantity == storefront.clicks == 0
    storefront.logged_in = False
    adapter.navigate(storefront.base + "/customer/account/login")
    runner, store = run_from_product(adapter, policy, storefront, tmp_path)
    outcomes = []
    task = threading.Thread(target=lambda: outcomes.append(runner.run()))
    task.start()
    try:
        while runner.events.get(timeout=5).phase is not TaskPhase.AWAITING_MANUAL_LOGIN:
            pass
        assert storefront.quantity == 0
        # The user completes the challenge in the same visible page. Keep the driver
        # command serialized with the runner's read-only login check.
        original = adapter.read_login_state

        def complete_once():
            state = original()
            if state.challenge:
                adapter._driver.find_element("link text", "Complete login").click()
            return state

        adapter.read_login_state = complete_once
        task.join(20)
        assert not task.is_alive()
        assert outcomes[0].phase is TaskPhase.SUCCEEDED, outcomes[0].message
        assert outcomes[0].login_preparation.mode == "MANUAL"
        assert store.load().purchase.state is SubmissionState.SUCCEEDED
    finally:
        runner.cancel_event.set()
        task.join(10)


def test_managed_visible_uc_cross_frame_flow(chromedriver_path, chrome_path, tmp_path, storefront):
    from topps_auto_bot.browser import verify_blank_browser
    from topps_auto_bot.driver_setup import (
        DriverOperation, check_enabled_driver, current_platform_tag, driver_binary_name, inspect_chrome,
        patch_driver_for_uc, prepare_manual_driver,
    )
    from topps_auto_bot.settings import AppPaths, load_driver_selection, save_driver_selection

    paths = AppPaths.under(tmp_path / "uc-data")
    paths.ensure_directories()
    selection = paths.drivers / "selection.json"
    chrome = inspect_chrome(chrome_path)
    source = tmp_path / driver_binary_name(current_platform_tag())
    shutil.copy2(chromedriver_path, source)
    original = source.read_bytes()
    activated = prepare_manual_driver(
        source=source, chrome=chrome, paths=paths,
        operation=DriverOperation("controlled-uc"), browser_verifier=verify_blank_browser,
        driver_preparer=patch_driver_for_uc, store_selection=lambda driver: save_driver_selection(selection, driver),
    )
    assert source.read_bytes() == original
    settings = load_driver_selection(selection)
    assert check_enabled_driver(settings, chrome=chrome).ok
    contents = activated.path.read_bytes()
    storefront.mode = "delay"
    policy = TrustedSourcePolicy(
        allowed_payment_origins=frozenset({storefront.base, *storefront.frames.values()}),
        allow_loopback_http=True,
    )
    profile = replace(DEFAULT_TOPPs_PROFILE, home_url=storefront.base + "/", cart_url=storefront.base + "/checkout/cart")
    adapter = open_live_session(
        chrome_path=str(chrome_path), driver_path=str(activated.path), profile_dir=paths.browser_profile,
        policy=policy, profile=profile,
    )
    try:
        assert "--disable-blink-features=AutomationControlled" in adapter.launch_arguments()
        assert "--no-sandbox" not in adapter.launch_arguments()
        assert not any("--headless" in arg for arg in adapter.launch_arguments())
        adapter.navigate(storefront.base + "/")
        runner, store = run_from_product(adapter, policy, storefront, tmp_path)
        outcome = runner.run()
        assert outcome.phase is TaskPhase.SUCCEEDED, outcome.message
        assert storefront.clicks == 1
        assert not store.load().blocking
    finally:
        adapter.close()
    assert activated.path.read_bytes() == contents
    diagnostic = open_live_session(
        chrome_path=str(chrome.path), driver_path=str(activated.path),
        profile_dir=tmp_path / "sandbox-diagnostic", policy=policy, no_sandbox=True,
    )
    try:
        assert "--no-sandbox" in diagnostic.launch_arguments()
    finally:
        diagnostic.close()
    activated.path.write_bytes(contents + b"tamper")
    assert not check_enabled_driver(settings, chrome=chrome).ok
