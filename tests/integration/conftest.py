from __future__ import annotations

import os
import threading
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from topps_auto_bot.browser import (
    PageProfile,
    TrustedSourcePolicy,
    open_live_session,
)
from topps_auto_bot.driver_setup import current_platform_tag, detect_chrome


def cached_driver() -> Path | None:
    configured = os.environ.get("TOPPS_TEST_CHROMEDRIVER")
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    name = "chromedriver.exe" if current_platform_tag().startswith("win") else "chromedriver"
    path = Path.home() / ".cache" / "topps-auto-bot-test" / f"{current_platform_tag()}-{name}"
    return path if path.is_file() else None


@pytest.fixture(scope="session")
def chromedriver_path() -> Path:
    path = cached_driver()
    if path is None:
        pytest.skip("未找到测试 chromedriver；先运行 scripts/fetch_test_driver.py 或设置 TOPPS_TEST_CHROMEDRIVER")
    return path


@pytest.fixture(scope="session")
def chrome_path() -> Path:
    chrome = detect_chrome()
    if chrome is None:
        pytest.skip("本机未找到 Chrome")
    return chrome.path


LOGIN_EMAIL = "login-synthetic@example.test"
LOGIN_PASSWORD = "synthetic-login-password"

CART_LINE = """
<div data-testid="cart-line">
  <span data-field="title">Synthetic Card</span>
  <span data-field="variant">Hobby Box</span>
  <span data-field="quantity">2</span>
  <span data-field="unit-price">$20.00</span>
</div>
"""

CHECKOUT_LINE = """
<div data-testid="checkout-line">
  <span data-field="title">Synthetic Card</span>
  <span data-field="variant">Hobby Box</span>
  <span data-field="quantity">2</span>
  <span data-field="unit-price">$20.00</span>
</div>
"""

FORM_FIELDS = """
<input name="full_name" value="Synthetic Tester">
<input name="email" value="synthetic@example.test">
<input name="phone" value="555-0100">
<input name="address1" value="1 Synthetic Test Lane">
<input name="city" value="Test City">
<input name="region" value="TS">
<input name="postal_code" value="00000">
<input name="country" value="US">
<input name="card_number" value="4111111111111111">
<input name="expiry" value="12/2030">
<input name="cvv" value="123">
<input name="holder_name" value="Synthetic Tester">
"""

SHIPPING = """
<div data-testid="shipping-option">
  <input type="radio" name="shipping" value="standard" checked>
  <span data-field="service-name">Standard</span>
  <span data-field="service-amount">$10.00</span>
</div>
<div data-testid="shipping-option">
  <input type="radio" name="shipping" value="express">
  <span data-field="service-name">Express</span>
  <span data-field="service-amount">$25.00</span>
</div>
"""


def page(title: str, body: str, script: str = "", *, account: str | None = None) -> bytes:
    if account:
        account_html = (
            '<div data-testid="account-menu">Account '
            f'<span data-field="account-email">{account}</span></div>'
        )
    else:
        account_html = '<a data-action="login" href="/login.html">Sign in</a>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body>
{account_html}
{body}
<script>{script}</script>
</body></html>""".encode("utf-8")


class LocalSite:
    def __init__(self, base: str) -> None:
        self.base = base
        self.cart_items: list[dict] = []
        self._lock = threading.Lock()
        self.logged_in_email: str | None = LOGIN_EMAIL
        self.expected_email = LOGIN_EMAIL
        self.expected_password = LOGIN_PASSWORD
        self.login_submissions = 0
        self.challenge_next_login = False

    def reset(self) -> None:
        with self._lock:
            self.cart_items.clear()
        self.logged_in_email = LOGIN_EMAIL
        self.expected_email = LOGIN_EMAIL
        self.expected_password = LOGIN_PASSWORD
        self.login_submissions = 0
        self.challenge_next_login = False

    def logout(self) -> None:
        self.logged_in_email = None

    def complete_manual_login(self) -> None:
        self.logged_in_email = self.expected_email

    def render(self, title: str, body: str, script: str = "", *, account: str | None | object = ...) -> bytes:
        if account is ...:
            account = self.logged_in_email
        return page(title, body, script, account=account)  # type: ignore[arg-type]

    def add_item(self, title: str, variant: str, quantity: int) -> None:
        with self._lock:
            self.cart_items.append(
                {"title": title, "variant": variant, "quantity": max(1, quantity), "unit_price": "$20.00"}
            )

    def cart_lines_html(self) -> str:
        with self._lock:
            items = list(self.cart_items)
        return "".join(
            f"""<div data-testid="cart-line">
  <span data-field="title">{item['title']}</span>
  <span data-field="variant">{item['variant']}</span>
  <span data-field="quantity">{item['quantity']}</span>
  <span data-field="unit-price">{item['unit_price']}</span>
</div>"""
            for item in items
        )

    def html(self, path: str) -> bytes:
        base = self.base
        if path.startswith("/login.html"):
            query = parse_qs(urlsplit(path).query)
            if self.logged_in_email:
                body = f"""<div data-page="login">
  <p>Signed in as {self.logged_in_email}</p>
  <a href="/flow-cart.html">Continue</a>
</div>"""
            else:
                error = (
                    '<p data-login-error="true">Invalid credentials</p>'
                    if query.get("error")
                    else ""
                )
                challenge = (
                    '<p data-login-challenge="true">Verification required</p>'
                    if query.get("challenge")
                    else ""
                )
                body = f"""<div data-page="login">
  {error}
  {challenge}
  <form action="/login-submit" method="get">
    <input name="login_email" type="email" autocomplete="username">
    <input name="login_password" type="password" autocomplete="current-password">
    <button data-action="login-submit" type="submit">Sign in</button>
  </form>
</div>"""
            return self.render(
                "login",
                body,
                "setInterval(function(){fetch('/login-status').then(function(r){return r.text()}).then(function(t){if(t==='1'){window.location.href='/flow-cart.html';}})},100);",
                account=None,
            )
        if path == "/login-status":
            return b"1" if self.logged_in_email else b"0"
        if path == "/cart.html":
            return self.render(
                "cart",
                f"""<div data-page="cart">
  <span data-field="currency">USD</span>
  {CART_LINE}
  <a data-action="checkout" href="/checkout.html">Checkout</a>
</div>""",
            )
        if path == "/cart-delayed.html":
            return self.render(
                "cart delayed",
                f"""<div data-page="cart">
  <span data-field="currency">USD</span>
  {CART_LINE}
  <div id="entry"></div>
</div>""",
                "setTimeout(function(){var a=document.createElement('a');a.setAttribute('data-action','checkout');a.href='/checkout.html';a.textContent='Checkout';document.getElementById('entry').appendChild(a);}, 400);",
            )
        if path == "/checkout.html":
            return self.render(
                "checkout",
                f"""<div data-page="checkout">
  <span data-field="currency">USD</span>
  {CHECKOUT_LINE}
  <div data-field="subtotal">$40.00</div>
  <div data-field="tax">$5.00</div>
  <div data-field="shipping-total">$10.00</div>
  <div data-field="total">$55.00</div>
  {SHIPPING}
  {FORM_FIELDS}
  <button data-action="submit-purchase" onclick="window.location.href='/confirmation.html'">Place order</button>
</div>""",
            )
        if path == "/checkout-empty-form.html":
            return self.render(
                "checkout empty form",
                f"""<div data-page="checkout">
  <span data-field="currency">USD</span>
  {CHECKOUT_LINE}
  <div data-field="subtotal">$40.00</div>
  <div data-field="tax">$5.00</div>
  <div data-field="shipping-total">$10.00</div>
  <div data-field="total">$55.00</div>
  {SHIPPING}
  <input name="full_name"><input name="email"><input name="phone"><input name="address1">
  <input name="city"><input name="region"><input name="postal_code"><input name="country">
  <input name="card_number"><input name="expiry"><input name="cvv"><input name="holder_name">
  <button data-action="submit-purchase" onclick="window.location.href='/confirmation.html'">Place order</button>
</div>""",
            )
        if path == "/checkout-disabled.html":
            return self.render(
                "checkout disabled",
                f"""<div data-page="checkout">
  <span data-field="currency">USD</span>
  {CHECKOUT_LINE}
  <div data-field="subtotal">$40.00</div>
  <div data-field="tax">$5.00</div>
  <div data-field="shipping-total">$10.00</div>
  <div data-field="total">$55.00</div>
  {SHIPPING}
  {FORM_FIELDS}
</div>""",
            )
        if path == "/confirmation.html":
            return self.render(
                "confirmation",
                f"""<div data-page="order-confirmation">
  <span data-field="order-number">ORDER-LOCAL-1</span>
  <span data-field="payment-status">paid</span>
  {CHECKOUT_LINE.replace('data-testid="checkout-line"', 'data-testid="order-line"')}
</div>""",
            )
        if path == "/flow-product.html":
            return self.render(
                "product",
                """<div data-page="product">
  <h1 data-field="product-title">Synthetic Card</h1>
  <form action="/add" method="get">
    <input type="hidden" name="title" value="Synthetic Card">
    <select data-field="variant-select" name="variant">
      <option value="Hobby Box">Hobby Box</option>
      <option value="Retail Box">Retail Box</option>
    </select>
    <input data-field="quantity-input" name="qty" type="number" value="1" min="1">
    <span data-field="product-price">$20.00</span>
    <button data-action="add-to-cart" type="submit">Add to cart</button>
  </form>
</div>""",
            )
        if path == "/flow-cart.html":
            lines = self.cart_lines_html()
            if not lines:
                body = """<div data-page="cart">
  <span data-field="currency">USD</span>
  <p data-cart-empty="true">Your cart is empty.</p>
</div>"""
            else:
                body = f"""<div data-page="cart">
  <span data-field="currency">USD</span>
  {lines}
  <a data-action="checkout" href="/flow-checkout.html">Checkout</a>
</div>"""
            return self.render("cart", body)
        if path == "/flow-checkout.html":
            with self._lock:
                items = list(self.cart_items)
            lines = "".join(
                f"""<div data-testid="checkout-line">
  <span data-field="title">{item['title']}</span>
  <span data-field="variant">{item['variant']}</span>
  <span data-field="quantity">{item['quantity']}</span>
  <span data-field="unit-price">{item['unit_price']}</span>
</div>"""
                for item in items
            )
            total_quantity = sum(item["quantity"] for item in items)
            subtotal = Decimal("20.00") * total_quantity
            tax = Decimal("5.00")
            shipping = Decimal("10.00")
            total = subtotal + tax + shipping
            return self.render(
                "checkout",
                f"""<div data-page="checkout">
  <span data-field="currency">USD</span>
  {lines}
  <div data-field="subtotal">${subtotal:.2f}</div>
  <div data-field="tax">${tax:.2f}</div>
  <div data-field="shipping-total">${shipping:.2f}</div>
  <div data-field="total">${total:.2f}</div>
  {SHIPPING}
  {FORM_FIELDS}
  <button data-action="submit-purchase" onclick="window.location.href='/flow-confirmation.html'">Place order</button>
</div>""",
            )
        if path == "/flow-confirmation.html":
            with self._lock:
                items = list(self.cart_items)
            lines = "".join(
                f"""<div data-testid="order-line">
  <span data-field="title">{item['title']}</span>
  <span data-field="variant">{item['variant']}</span>
  <span data-field="quantity">{item['quantity']}</span>
  <span data-field="unit-price">{item['unit_price']}</span>
</div>"""
                for item in items
            )
            return self.render(
                "confirmation",
                f"""<div data-page="order-confirmation">
  <span data-field="order-number">ORDER-FLOW-1</span>
  <span data-field="payment-status">paid</span>
  {lines}
</div>""",
            )
        if path == "/product.html":
            return self.render(
                "product",
                """<div data-page="product">
  <h1 data-field="product-title">Synthetic Card</h1>
  <span data-field="product-variant">Hobby Box</span>
  <span data-field="product-variant">Retail Box</span>
</div>""",
            )
        if path == "/":
            return self.render("home", '<form action="/" method="get"><input type="search" name="q"></form>')
        if path.startswith("/?q="):
            return self.render(
                "search",
                """<div data-page="search">
  <div data-testid="search-result">
    <span data-field="result-title">Synthetic Card</span>
    <a href="/product.html">查看</a>
    <span data-field="product-variant">Hobby Box</span>
  </div>
  <div data-testid="search-result">
    <span data-field="result-title">Synthetic Card Variant</span>
    <a href="/product.html">查看</a>
    <span data-field="product-variant">Retail Box</span>
  </div>
</div>""",
            )
        if path == "/payment.html":
            return self.render("payment", "<div>payment frame</div>")
        return self.render("not found", "<div>not found</div>")


@pytest.fixture(scope="session")
def local_site():
    site_holder: dict[str, LocalSite] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            site = site_holder["site"]
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if self.path.startswith("/login-submit"):
                query = parse_qs(urlsplit(self.path).query)
                site.login_submissions += 1
                email = (query.get("login_email") or [""])[0]
                password = (query.get("login_password") or [""])[0]
                if site.challenge_next_login:
                    site.challenge_next_login = False
                    location = "/login.html?challenge=1"
                elif email == site.expected_email and password == site.expected_password:
                    site.logged_in_email = email
                    location = "/flow-cart.html"
                else:
                    location = "/login.html?error=1"
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()
                return
            if self.path.startswith("/add"):
                query = parse_qs(urlsplit(self.path).query)
                title = (query.get("title") or ["Synthetic Card"])[0]
                variant = (query.get("variant") or ["Hobby Box"])[0]
                raw_quantity = (query.get("qty") or ["1"])[0]
                quantity = int(raw_quantity) if raw_quantity.isdigit() else 1
                site.add_item(title, variant, quantity)
                self.send_response(302)
                self.send_header("Location", "/flow-cart.html")
                self.end_headers()
                return
            payload = site.html(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    site = LocalSite(base)
    site_holder["site"] = site
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def browser(chromedriver_path: Path, chrome_path: Path, local_site: LocalSite, tmp_path: Path):
    origin = local_site.base
    policy = TrustedSourcePolicy(
        allowed_payment_origins=frozenset({origin}),
        allow_loopback_http=True,
    )
    profile = PageProfile(
        name="local-test",
        home_url=origin + "/",
        cart_url=origin + "/flow-cart.html",
        login_url=origin + "/login.html",
    )
    adapter = open_live_session(
        chrome_path=str(chrome_path),
        driver_path=str(chromedriver_path),
        profile_dir=tmp_path / "browser-profile",
        policy=policy,
        profile=profile,
        page_load_timeout=30.0,
        headless=True,
        use_uc=False,
    )
    yield adapter, policy, local_site
    adapter.close()
