from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from topps_auto_bot.browser import (
    DEFAULT_TOPPs_PROFILE,
    _SandboxedUCOptions,
    PageProfile,
    SeleniumPageAdapter,
    TrustedSourcePolicy,
    detect_currency,
    parse_display_money,
    parse_display_quantity,
)
from topps_auto_bot.models import LoginCredentials, LoginStatus, PageKind, ReadStatus


class EmptyDriver:
    current_url = "https://www.topps.com/checkout/cart"

    def find_elements(self, by, value):
        return []


def test_page_source_allow_list_is_exact() -> None:
    policy = TrustedSourcePolicy()
    assert policy.check_page_url("https://www.topps.com/checkout/cart").ok
    assert policy.check_page_url("https://topps.com/products/x").ok
    for url in (
        "http://www.topps.com/cart",
        "https://www.topps.com.example.invalid/cart",
        "https://www.topps.com:8443/cart",
        "https://user:pass@www.topps.com/cart",
        "https://nottopps.com/cart",
    ):
        assert not policy.check_page_url(url).ok


def test_payment_origins_require_explicit_allow_list() -> None:
    policy = TrustedSourcePolicy(allowed_payment_origins=frozenset({"https://pay.topps.example"}))
    assert policy.check_payment_url("https://pay.topps.example/frame").ok
    assert not policy.check_payment_url("https://pay.topps.example.evil.invalid/frame").ok
    assert not policy.check_payment_url("http://pay.topps.example/frame").ok
    default = TrustedSourcePolicy()
    assert not default.check_payment_url("https://pay.topps.example/frame").ok


def test_login_origins_require_explicit_allow_list() -> None:
    default = TrustedSourcePolicy()
    assert not default.check_login_url("https://accounts.example.test/login").ok
    assert default.check_login_url("https://www.topps.com/account/login").ok
    policy = TrustedSourcePolicy(allowed_login_origins=frozenset({"https://accounts.example.test"}))
    assert policy.check_login_url("https://accounts.example.test/login").ok
    assert not policy.check_login_url("https://accounts.example.test.evil.invalid/login").ok
    assert not policy.check_login_url("http://accounts.example.test/login").ok


def test_loopback_test_policy_is_explicit() -> None:
    policy = TrustedSourcePolicy(
        allowed_payment_origins=frozenset({"http://127.0.0.1:8080"}),
        allow_loopback_http=True,
    )
    assert policy.check_page_url("http://127.0.0.1:8080/cart").ok
    assert policy.check_payment_url("http://127.0.0.1:8080/frame").ok
    assert not policy.check_page_url("http://evil.invalid/cart").ok


def test_money_display_parsing() -> None:
    assert parse_display_money("$1,234.56") == Decimal("1234.56")
    assert parse_display_money("USD 10.00") == Decimal("10.00")
    assert parse_display_money("Free") is None
    assert parse_display_money("10.999") is None
    assert parse_display_quantity("Qty: 2") == 2
    assert parse_display_quantity("none") is None
    assert detect_currency("USD $55.00") == "USD"
    assert detect_currency("EUR 55.00") == "EUR"


def test_default_profile_checks_actual_fields_and_does_not_assume_empty_cart() -> None:
    adapter = SeleniumPageAdapter(EmptyDriver(), DEFAULT_TOPPs_PROFILE, TrustedSourcePolicy())
    assert adapter.page_kind() is PageKind.CART
    assert adapter.is_logged_in() is None
    assert adapter.read_login_state().status is LoginStatus.UNRECOGNIZED
    assert adapter.read_cart().status is ReadStatus.NOT_AVAILABLE
    assert adapter.read_checkout().status is ReadStatus.NOT_AVAILABLE
    assert adapter.read_product().status is ReadStatus.UNRECOGNIZED
    assert not adapter.enter_checkout().ok
    assert not adapter.submit_purchase().attempted


def test_uc_sandbox_option_wrapper_does_not_add_no_sandbox() -> None:
    options = _SandboxedUCOptions()
    options.add_argument("--no-sandbox")
    assert "--no-sandbox" not in options.arguments
    options.add_argument("--disable-blink-features=AutomationControlled")
    assert "--disable-blink-features=AutomationControlled" in options.arguments
