from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .models import (
    ActionResult,
    Card,
    CartRead,
    CheckoutRead,
    Customer,
    FormState,
    LoginCredentials,
    LoginRead,
    LoginStatus,
    OrderLine,
    OrderResult,
    OrderStatus,
    PageKind,
    ProductCandidate,
    ProductPageState,
    ProductRead,
    ProductSearchRead,
    ReadStatus,
    ShippingOption,
    SubmitActionResult,
    USD,
    normalize_text,
    format_usd,
)

TOPPs_HOSTS = frozenset({"topps.com", "www.topps.com"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


@dataclass(frozen=True)
class SourceCheck:
    ok: bool
    url: str
    origin: str | None
    reason: str | None = None


@dataclass(frozen=True)
class TrustedSourcePolicy:
    allowed_page_hosts: frozenset[str] = TOPPs_HOSTS
    allowed_payment_origins: frozenset[str] = frozenset({
        "https://www.topps.com", "https://topps.com", "https://checkout.shopifycs.com",
    })
    allowed_login_origins: frozenset[str] = frozenset({"https://id.fanatics.com"})
    allow_loopback_http: bool = False

    def _origin(self, parsed) -> str | None:
        if not parsed.hostname:
            return None
        host = parsed.hostname.casefold()
        scheme = parsed.scheme.casefold()
        port = parsed.port
        default_port = 443 if scheme == "https" else 80
        if port is None:
            return f"{scheme}://{host}"
        if port == default_port:
            return f"{scheme}://{host}"
        return f"{scheme}://{host}:{port}"

    def check_page_url(self, url: str) -> SourceCheck:
        return self._check(url, payment=False)

    def check_payment_url(self, url: str) -> SourceCheck:
        return self._check(url, payment=True)

    def check_login_url(self, url: str) -> SourceCheck:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return SourceCheck(False, url, None, "URL 无法解析")
        host = (parsed.hostname or "").casefold()
        scheme = parsed.scheme.casefold()
        if parsed.username is not None or parsed.password is not None:
            return SourceCheck(False, url, None, "URL 不能包含用户信息")
        loopback = self.allow_loopback_http and host in LOOPBACK_HOSTS and scheme == "http"
        if scheme != "https" and not loopback:
            return SourceCheck(False, url, None, "登录来源必须使用 HTTPS")
        try:
            port = parsed.port
        except ValueError:
            return SourceCheck(False, url, None, "URL 端口无效")
        if not loopback and port not in (None, 443):
            return SourceCheck(False, url, None, "登录来源只能使用默认 HTTPS 端口")
        origin = self._origin(parsed)
        allowed = loopback or host in self.allowed_page_hosts or (origin or "") in self.allowed_login_origins
        if not allowed:
            return SourceCheck(False, url, origin, "登录来源不在已核实白名单中")
        return SourceCheck(True, url, origin)

    def _check(self, url: str, *, payment: bool) -> SourceCheck:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return SourceCheck(False, url, None, "URL 无法解析")
        host = (parsed.hostname or "").casefold()
        scheme = parsed.scheme.casefold()
        if parsed.username is not None or parsed.password is not None:
            return SourceCheck(False, url, None, "URL 不能包含用户信息")
        loopback = self.allow_loopback_http and host in LOOPBACK_HOSTS and scheme == "http"
        if scheme != "https" and not loopback:
            return SourceCheck(False, url, None, "来源必须使用 HTTPS")
        try:
            port = parsed.port
        except ValueError:
            return SourceCheck(False, url, None, "URL 端口无效")
        if not loopback and port not in (None, 443):
            return SourceCheck(False, url, None, "来源只能使用默认 HTTPS 端口")
        allowed = host in self.allowed_page_hosts or loopback
        if payment:
            origin = self._origin(parsed)
            allowed = origin in self.allowed_payment_origins if not loopback else (origin or "") in self.allowed_payment_origins
        if not allowed:
            return SourceCheck(False, url, self._origin(parsed), "来源不在已核实白名单中")
        return SourceCheck(True, url, self._origin(parsed))


def ensure_trusted_page(policy: TrustedSourcePolicy, url: str) -> SourceCheck:
    return policy.check_page_url(url)


def ensure_trusted_payment_origin(policy: TrustedSourcePolicy, origin_or_url: str | None) -> SourceCheck:
    if not origin_or_url:
        return SourceCheck(False, "", None, "无法确认支付页面来源")
    return policy.check_payment_url(origin_or_url)


@dataclass(frozen=True)
class PageProfile:
    name: str
    login_selector: str = "[data-testid='account-menu']"
    account_identity: str = "[data-field='account-email']"
    login_url: str = ""
    login_page_root: str = "[data-page='login']"
    login_email_field: str = "[name='login_email']"
    login_password_field: str = "[name='login_password']"
    login_submit_button: str = "[data-action='login-submit']"
    login_error: str = "[data-login-error]"
    login_challenge: str = "[data-login-challenge]"
    blocked_selector: str = "[data-testid='access-blocked']"
    cart_path_hint: str = "/cart"
    checkout_path_hint: str = "checkout"
    cart_root: str = "[data-page='cart']"
    cart_empty: str = "[data-cart-empty]"
    cart_line: str = "[data-testid='cart-line']"
    line_title: str = "[data-field='title']"
    line_variant: str = "[data-field='variant']"
    line_quantity: str = "[data-field='quantity']"
    line_unit_price: str = "[data-field='unit-price']"
    currency_selector: str = "[data-field='currency']"
    cart_checkout_button: str = "[data-action='checkout']"
    checkout_root: str = "[data-page='checkout']"
    checkout_line: str = "[data-testid='checkout-line']"
    subtotal_selector: str = "[data-field='subtotal']"
    tax_selector: str = "[data-field='tax']"
    shipping_total_selector: str = "[data-field='shipping-total']"
    total_selector: str = "[data-field='total']"
    shipping_option: str = "[data-testid='shipping-option']"
    shipping_option_input: str = "input[type='radio']"
    shipping_option_name: str = "[data-field='service-name']"
    shipping_option_amount: str = "[data-field='service-amount']"
    final_submit_button: str = "[data-action='submit-purchase']"
    contact_fields: tuple[str, ...] = (
        "[name='full_name']",
        "[name='email']",
        "[name='phone']",
        "[name='address1']",
        "[name='city']",
        "[name='region']",
        "[name='postal_code']",
    )
    card_fields: tuple[str, ...] = (
        "[name='card_number']",
        "[name='expiry']",
        "[name='cvv']",
    )
    card_holder_field: str = "input[name='holder_name']"
    payment_root: str = ""
    payment_iframe: str = "iframe[data-payment-frame]"
    card_frames: tuple[str, ...] = (
        "iframe[data-payment-frame='number']", "iframe[data-payment-frame='expiry']",
        "iframe[data-payment-frame='cvv']", "iframe[data-payment-frame='holder']",
    )
    bank_challenge: str = "iframe[title*='3D Secure' i], iframe[title*='bank verification' i]"
    full_name_fields: tuple[str, str] = ("input[autocomplete='given-name']", "input[autocomplete='family-name']")
    address2_field: str = "input[name='address2']"
    country_field: str = "[name='country']"
    order_root: str = "[data-page='order-confirmation']"
    order_number: str = "[data-field='order-number']"
    payment_status: str = "[data-field='payment-status']"
    declined_selector: str = "[data-state='declined']"
    processing_selector: str = "[data-state='processing']"
    order_lines: str = "[data-testid='order-line']"
    product_root: str = "[data-page='product']"
    product_path_prefix: str = ""
    queue_status: str = "#checkout-queue, [role='status'][aria-label='Waiting room' i]"
    product_title: str = "[data-field='product-title']"
    product_variant_option: str = "[data-field='product-variant']"
    product_variant_select: str = "select[data-field='variant-select']"
    product_quantity_input: str = "input[data-field='quantity-input']"
    product_unit_price: str = "[data-field='product-price']"
    product_add_button: str = "[data-action='add-to-cart']"
    product_sold_out: str = "[data-state='sold-out']"
    product_add_error: str = "[data-add-error]"
    cart_url: str = "https://www.topps.com/checkout/cart"
    home_url: str = "https://www.topps.com/"
    search_input: str = "form[action='/'] input[name='q'], input[type='search'][name='q']"
    search_toggle: str = "button[aria-label='Search' i]"
    search_stock: str = "input[type='checkbox'][name='in-stock'], button[aria-label='In Stock Only' i]"
    search_empty: str = "[role='status']"
    search_result_is_link: bool = False
    search_root: str = "[data-page='search']"
    search_result: str = "[data-testid='search-result']"
    search_result_title: str = "[data-field='result-title']"
    search_result_link: str = "a[href]"


# Topps storefront and Shopify hosted checkout. Test profiles can supply isolated rules.
DEFAULT_TOPPs_PROFILE = PageProfile(
    name="topps-us",
    login_url="https://www.topps.com/customer/account/login?next=%2F",
    login_selector=(
        "a[href='/customer/account/logout'], button[aria-label='Sign out' i], "
        "a[href='/api/auth/signout'], #checkout-contact [aria-label='Signed in account' i]"
    ),
    account_identity="#account-email, #checkout-contact [aria-label='Signed in account' i]",
    login_page_root="form:has(input[autocomplete='username']), form:has(input[autocomplete='one-time-code'])",
    login_email_field="input[type='email'], input[autocomplete='username']",
    login_password_field="input[autocomplete='current-password']",
    login_submit_button="form:has(input[autocomplete='username']) button[type='submit']",
    login_error="form [role='alert']",
    login_challenge="input[autocomplete='one-time-code'], #challenge-stage, iframe[title*='challenge' i]",
    blocked_selector="#cf-error-details, #cf-error-overview",
    checkout_path_hint="/checkouts/",
    cart_root="main",
    cart_empty="h1, h2",
    cart_line="main li:has(input[name*='quantity' i]), [data-testid='cart-item']",
    line_title="[itemprop='name'], a[href*='/products/'], .product__description__name",
    line_variant="[itemprop='model'], .product__description__variant, [aria-label='Variant' i]",
    line_quantity="input[name*='quantity' i], [aria-label^='Quantity' i], .product__quantity",
    line_unit_price="[aria-label='Unit price' i], [itemprop='price'], .product__price",
    currency_selector="[itemprop='priceCurrency'], [aria-label='Currency' i]",
    cart_checkout_button="button[name='checkout'], a[href^='/checkouts/'], [data-testid='checkout-button']",
    checkout_root="main:has(#checkout-pay-button, #checkout-payment, #payment, #checkout-main)",
    checkout_line="[role='row']:has(.product__description__name), .product-table tbody tr, [aria-label='Order summary' i] li:has([itemprop='name'])",
    subtotal_selector=".total-line--subtotal .total-line__price, [aria-label='Subtotal' i]",
    tax_selector=".total-line--taxes .total-line__price, [aria-label='Estimated taxes' i], [aria-label='Taxes' i]",
    shipping_total_selector=".total-line--shipping .total-line__price, [aria-label='Shipping total' i]",
    total_selector=".payment-due__price, [aria-label='Total' i]",
    shipping_option="label:has(input[type='radio'][name*='shipping' i]), .content-box__row:has(input[type='radio'][name*='shipping' i])",
    shipping_option_name=".radio__label__primary, [aria-label='Shipping service' i]",
    shipping_option_amount=".radio__label__accessory, [aria-label='Shipping price' i]",
    final_submit_button="#checkout-pay-button, button[name='complete_order']",
    contact_fields=(
        "input[autocomplete='name']", "input[autocomplete~='email'], input#email",
        "input[autocomplete~='tel']", "input[autocomplete~='address-line1']",
        "input[autocomplete~='address-level2']", "[autocomplete~='address-level1']",
        "input[autocomplete~='postal-code']",
    ),
    address2_field="input[autocomplete~='address-line2']",
    country_field="select[autocomplete~='country'], select[name='countryCode']",
    card_fields=(
        "input[autocomplete~='cc-number'], input#number",
        "input[autocomplete~='cc-exp'], input#expiry",
        "input[autocomplete~='cc-csc'], input#verification_value",
    ),
    card_holder_field="input[autocomplete~='cc-name'], input#name",
    payment_root="#payment, #checkout-payment, [aria-label='Payment' i]",
    payment_iframe="iframe[id^='card-fields-'], iframe[src*='checkout.shopifycs.com']",
    card_frames=(
        "iframe[id^='card-fields-number'], iframe[title='Card number' i]",
        "iframe[id^='card-fields-expiry'], iframe[title*='Expiration date' i]",
        "iframe[id^='card-fields-verification_value'], iframe[title*='Security code' i]",
        "iframe[id^='card-fields-name'], iframe[title='Name on card' i]",
    ),
    order_root="main:has(#confirmation-number, .os-order-number)",
    order_number="#confirmation-number, .os-order-number",
    payment_status="[aria-label='Payment status' i], .payment-status",
    order_lines=".product-table tbody tr, [aria-label='Order summary' i] li:has([itemprop='name'])",
    declined_selector="#payment [role='alert'][id*='declined' i]",
    processing_selector="[role='status'][aria-label='Processing payment' i]",
    product_root="main",
    product_path_prefix="/products/",
    product_title="h1, [itemprop='name']",
    product_variant_option="[role='option'], button[aria-pressed][name='variant']",
    product_variant_select="select[name='id'], select[name='variant']",
    product_quantity_input="input[name='quantity'], input[aria-label='Quantity' i]",
    product_unit_price="[itemprop='price'], [data-testid='product-price']",
    product_add_button="button[data-testid='product-add-to-cart'], button[name='add']",
    product_sold_out="button[data-sold-out='true']",
    product_add_error="[role='alert']",
    search_root="#search-results, [aria-label='Search results' i], [role='dialog'][aria-label*='search' i]",
    search_result="a[href*='/products/']",
    search_result_title="h2, h3, [itemprop='name']",
    search_result_is_link=True,
)


class PurchaseSession(Protocol):
    def current_url(self) -> str: ...

    def page_kind(self) -> PageKind: ...

    def is_logged_in(self) -> bool | None: ...

    def read_login_state(self) -> LoginRead: ...

    def login_with_credentials(self, login: LoginCredentials) -> ActionResult: ...

    def navigate_to_cart(self) -> ActionResult: ...

    def open_product(self, url: str) -> ProductPageState: ...

    def read_product_page(self) -> ProductPageState: ...

    def select_variant(self, variant: str) -> ActionResult: ...

    def set_quantity(self, quantity: int) -> ActionResult: ...

    def add_to_cart(self, *, check: Callable[[], None] = lambda: None) -> ActionResult: ...

    def read_cart(self) -> CartRead: ...

    def read_checkout(self) -> CheckoutRead: ...

    def enter_checkout(self) -> ActionResult: ...

    def fill_checkout(
        self,
        customer: Customer,
        card: Card,
        policy: TrustedSourcePolicy,
        *,
        check: Callable[[], None] = lambda: None,
    ) -> ActionResult: ...

    def submit_purchase(self, *, check: Callable[[], None] = lambda: None) -> SubmitActionResult: ...

    def read_order_result(self) -> OrderResult: ...

    def refresh(self) -> None: ...

    def close(self) -> None: ...


_MONEY = re.compile(r"(?<![\d.,+-])(?:USD|\$)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)(?![0-9.])", re.IGNORECASE)


def parse_display_money(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = text.strip()
    match = _MONEY.search(cleaned)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        value = Decimal(raw)
    except Exception:  # noqa: BLE001 - display text is untrusted
        return None
    if value.as_tuple().exponent < -2:
        return None
    return value.quantize(Decimal("0.01"))


def _read_usd_amount(text: str, *, allow_free: bool = False) -> Decimal | None:
    if detect_currency(text) not in (None, USD):
        return None
    return Decimal("0.00") if allow_free and text.strip().casefold() == "free" else parse_display_money(text)


def parse_display_quantity(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    value = int(match.group(0))
    return value if value > 0 else None


def detect_currency(text: str | None) -> str | None:
    if not text:
        return None
    upper = text.upper()
    match = re.search(r"\b(USD|CAD|AUD|NZD|GBP|EUR|JPY|CHF|MXN)\b", upper)
    if match:
        return match.group(1)
    if any(marker in upper for marker in ("€", "£", "¥", "CA$", "AU$", "C$")):
        return "NON_USD"
    return USD if "$" in upper else None


def _text(element) -> str:
    try:
        return (element.text or "").strip()
    except Exception:  # noqa: BLE001 - stale elements are treated as unreadable
        return ""


def _attr(element, name: str) -> str | None:
    try:
        return element.get_attribute(name)
    except Exception:  # noqa: BLE001
        return None


def _displayed(element) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:  # noqa: BLE001
        return False


def _find_first(root, selector: str):
    try:
        matches = root.find_elements("css selector", selector)
    except Exception:  # noqa: BLE001
        return None
    for element in matches:
        if _displayed(element):
            return element
    return None


def _find_all(root, selector: str) -> list:
    try:
        return [element for element in root.find_elements("css selector", selector) if _displayed(element)]
    except Exception:  # noqa: BLE001
        return []


def _enabled(element) -> bool:
    return bool(element is not None and element.is_enabled() and _attr(element, "aria-disabled") != "true")


class _CheckoutFieldError(Exception):
    pass


class _CheckoutFieldPending(_CheckoutFieldError):
    pass


def _js_click(driver, element) -> None:
    driver.execute_script("arguments[0].click();", element)


def _read_line(line_element, profile: PageProfile) -> OrderLine | None:
    title = _text(_find_first(line_element, profile.line_title))
    variant = _text(_find_first(line_element, profile.line_variant))
    quantity_node = _find_first(line_element, profile.line_quantity)
    quantity = parse_display_quantity(_attr(quantity_node, "value") or _text(quantity_node) or _attr(quantity_node, "aria-label"))
    price = _read_usd_amount(_text(_find_first(line_element, profile.line_unit_price)))
    if not title or quantity is None:
        return None
    return OrderLine(
        title=title,
        variant=variant,
        quantity=quantity,
        unit_price_usd=price,
    )


class SeleniumPageAdapter:
    def __init__(
        self,
        driver,
        profile: PageProfile,
        policy: TrustedSourcePolicy,
        *,
        page_load_timeout: float = 30.0,
    ) -> None:
        self._driver = driver
        self._profile = profile
        self._policy = policy
        self._page_load_timeout = page_load_timeout
        self._filled_checkout = None
        self._hosted_checkout_url: str | None = None

    def current_url(self) -> str:
        return self._driver.current_url

    def navigate(self, url: str) -> None:
        source = self._policy.check_page_url(url)
        if not source.ok:
            raise ValueError(source.reason or "来源不可信")
        self._driver.get(url)

    def launch_arguments(self) -> tuple[str, ...]:
        return tuple(
            arg for arg in getattr(self._driver.options, "arguments", ())
            if not arg.startswith(("--user-data-dir=", "--remote-debugging-port="))
        )

    def public_access_error(self) -> str | None:
        if not self._source_ok().ok:
            return "官网首页跳转来源不可信"
        if _find_first(self._driver, self._profile.blocked_selector):
            return "官网访问受限"
        if _find_first(self._driver, self._profile.login_challenge):
            return "官网要求人工处理挑战"
        if not _find_first(self._driver, "main, header"):
            return "官网页面内容未能识别"
        return None

    def _source_ok(self) -> SourceCheck:
        return self._policy.check_page_url(self.current_url())

    def _login_source_ok(self) -> SourceCheck:
        page = self._policy.check_page_url(self.current_url())
        if page.ok:
            return page
        return self._policy.check_login_url(self.current_url())

    def page_kind(self) -> PageKind:
        url = urlsplit(self.current_url()).path.casefold()
        if self._profile.blocked_selector and _find_first(self._driver, self._profile.blocked_selector):
            return PageKind.BLOCKED
        if _find_first(self._driver, self._profile.checkout_root):
            return PageKind.CHECKOUT
        if self._profile.cart_path_hint in url:
            return PageKind.CART
        if self._profile.checkout_path_hint in url:
            return PageKind.CHECKOUT
        if _find_first(self._driver, self._profile.login_selector) is None and (
            "login" in url or "signin" in url or "sign-in" in url
        ):
            return PageKind.LOGIN
        if self._profile.cart_path_hint in url:
            return PageKind.CART
        return PageKind.OTHER

    def is_logged_in(self) -> bool | None:
        if not self._source_ok().ok:
            return None
        if self.page_kind() is PageKind.LOGIN:
            return False
        if _find_first(self._driver, self._profile.login_selector) is not None:
            return True
        if _find_first(self._driver, "a[href*='/customer/account/login'], a[href*='/login.html']"):
            return False
        return None

    def account_identity(self) -> str | None:
        element = _find_first(self._driver, self._profile.account_identity)
        if element is None:
            return None
        return _text(element) or _attr(element, "content") or None

    def read_login_state(self) -> LoginRead:
        source = self._login_source_ok()
        if not source.ok:
            return LoginRead(status=LoginStatus.BLOCKED, page_url=self.current_url(), error=source.reason)
        try:
            page = self._driver
            if _find_first(page, self._profile.blocked_selector):
                return LoginRead(status=LoginStatus.BLOCKED, page_url=self.current_url())
            if _find_first(page, self._profile.login_challenge):
                return LoginRead(
                    status=LoginStatus.CHALLENGE,
                    page_url=self.current_url(),
                    challenge=True,
                    error="官网要求验证码或额外认证",
                )
            login_root = _find_first(page, self._profile.login_page_root)
            if login_root is not None or self.page_kind() is PageKind.LOGIN:
                error = _text(_find_first(page, self._profile.login_error)) or None
                return LoginRead(
                    status=LoginStatus.NOT_LOGGED_IN,
                    page_url=self.current_url(),
                    error=error,
                )
            logged_in = self.is_logged_in()
            if logged_in is True:
                return LoginRead(
                    status=LoginStatus.LOGGED_IN,
                    account_identity=self.account_identity(),
                    page_url=self.current_url(),
                )
            if logged_in is False:
                return LoginRead(status=LoginStatus.NOT_LOGGED_IN, page_url=self.current_url())
            return LoginRead(status=LoginStatus.UNRECOGNIZED, page_url=self.current_url())
        except Exception as exc:  # noqa: BLE001
            return LoginRead(status=LoginStatus.ERROR, page_url=self.current_url(), error=str(exc))

    def login_with_credentials(self, login: LoginCredentials) -> ActionResult:
        if not self._profile.login_url.strip():
            return ActionResult(False, "尚未配置已核实的官方登录入口")
        source = self._policy.check_login_url(self._profile.login_url)
        if not source.ok:
            return ActionResult(False, source.reason or "登录来源不可信")
        try:
            self._driver.get(self._profile.login_url)
            final_source = self._login_source_ok()
            if not final_source.ok:
                return ActionResult(False, final_source.reason or "登录跳转来源不可信")
            state = self.read_login_state()
            if state.status is LoginStatus.CHALLENGE:
                return ActionResult(False, "官网要求验证码或额外认证", attempted=False)
            if state.status is LoginStatus.LOGGED_IN:
                return ActionResult(False, "当前会话已经登录，不重复提交凭据", attempted=False)
            root = _find_first(self._driver, self._profile.login_page_root)
            if root is None:
                return ActionResult(False, "未识别官方登录表单", attempted=False)
            email_field = _find_first(root, self._profile.login_email_field)
            if email_field is None:
                return ActionResult(False, "未找到已核实的登录账号输入框", attempted=False)
            if not self._login_source_ok().ok:
                return ActionResult(False, "登录表单来源已变化", attempted=False)
            form_action = self._driver.execute_script("return arguments[0].form?.action ?? null", email_field)
            if form_action and not self._policy.check_login_url(form_action).ok:
                return ActionResult(False, "登录表单提交来源不可信", attempted=False)
            email_field.clear()
            email_field.send_keys(login.email)
            password_field = _find_first(root, self._profile.login_password_field)
            if password_field is not None:
                password_field.clear()
                if login.password:
                    password_field.send_keys(login.password)
            submit = _find_first(root, self._profile.login_submit_button)
            if submit is None:
                return ActionResult(False, "未找到已核实的登录提交按钮", attempted=False)
            try:
                submit.click()
            except Exception as exc:  # noqa: BLE001 - click may have reached the site
                return ActionResult(False, str(exc), uncertain=True, attempted=True)
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def navigate_to_cart(self) -> ActionResult:
        source = self._policy.check_page_url(self._profile.cart_url)
        if not source.ok:
            return ActionResult(False, source.reason or "购物车来源不可信")
        try:
            self._driver.get(self._profile.cart_url)
            final_source = self._login_source_ok()
            if not final_source.ok:
                return ActionResult(False, final_source.reason or "购物车跳转来源不可信")
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def open_product(self, url: str) -> ProductPageState:
        source = self._policy.check_page_url(url)
        if not source.ok:
            return ProductPageState(status=ReadStatus.BLOCKED, error=source.reason)
        try:
            self._driver.get(url)
            return self.read_product_page()
        except Exception as exc:  # noqa: BLE001
            return ProductPageState(status=ReadStatus.ERROR, error=str(exc))

    def read_product_page(self) -> ProductPageState:
        source = self._source_ok()
        if not source.ok:
            return ProductPageState(status=ReadStatus.BLOCKED, url=self.current_url(), error=source.reason)
        try:
            page = self._driver
            if _find_first(page, self._profile.blocked_selector):
                return ProductPageState(status=ReadStatus.BLOCKED, url=self.current_url())
            if _find_first(page, self._profile.login_challenge):
                return ProductPageState(status=ReadStatus.CHALLENGE, error="请在专用 Chrome 中完成官网验证")
            wait_hint = _find_first(page, self._profile.queue_status)
            if wait_hint is not None:
                delay = _attr(wait_hint, "data-retry-after") or ""
                return ProductPageState(
                    status=ReadStatus.WAITING,
                    retry_after_seconds=int(delay) if re.fullmatch(r"[0-9]{1,8}", delay) else None,
                )
            if not urlsplit(self.current_url()).path.startswith(self._profile.product_path_prefix):
                return ProductPageState(status=ReadStatus.UNRECOGNIZED, error="未识别官网直购商品路径")
            root = _find_first(page, self._profile.product_root)
            if root is None:
                return ProductPageState(status=ReadStatus.UNRECOGNIZED, url=self.current_url())
            title = _text(_find_first(root, self._profile.product_title))
            if not title:
                return ProductPageState(status=ReadStatus.UNRECOGNIZED, url=self.current_url())
            select_element = _find_first(root, self._profile.product_variant_select)
            variants: tuple[str, ...] = ()
            selected_variant: str | None = None
            if select_element is not None:
                from selenium.webdriver.support.ui import Select

                selector = Select(select_element)
                variants = tuple(option.text.strip() for option in selector.options if option.text.strip())
                if not variants:
                    return ProductPageState(status=ReadStatus.UNRECOGNIZED, error="规格控件没有可核实的选项")
                selected_text = selector.first_selected_option.text.strip()
                selected_variant = selected_text or None
            else:
                variants = tuple(
                    text
                    for element in _find_all(root, self._profile.product_variant_option)
                    if (text := _text(element))
                )
                selected = [
                    element
                    for element in _find_all(root, self._profile.product_variant_option)
                    if (_attr(element, "aria-selected") in ("true", "checked") or _attr(element, "data-selected") == "true")
                ]
                if len(selected) == 1:
                    selected_variant = _text(selected[0]) or None
            quantity_element = _find_first(root, self._profile.product_quantity_input)
            quantity = None
            if quantity_element is not None:
                raw = _attr(quantity_element, "value") or _text(quantity_element)
                if raw and raw.strip().isdigit():
                    quantity = int(raw.strip())
            price_text = _text(_find_first(root, self._profile.product_unit_price))
            price = parse_display_money(price_text)
            currency = detect_currency(price_text)
            sold_out = _find_first(root, self._profile.product_sold_out) is not None
            button = _find_first(root, self._profile.product_add_button)
            if self._profile.product_path_prefix and button is None:
                return ProductPageState(status=ReadStatus.UNRECOGNIZED, error="未识别官网直购按钮")
            enabled = bool(button is not None and _displayed(button) and not sold_out)
            if button is not None:
                disabled = _attr(button, "disabled")
                aria_disabled = _attr(button, "aria-disabled")
                if disabled is not None or aria_disabled in ("true", "disabled"):
                    enabled = False
            return ProductPageState(
                status=ReadStatus.OK,
                title=title,
                url=self.current_url(),
                variants=variants,
                selected_variant=selected_variant,
                quantity=quantity,
                unit_price_usd=price,
                currency=currency,
                sold_out=sold_out,
                add_to_cart_enabled=enabled,
                single_variant=len(variants) <= 1,
            )
        except Exception as exc:  # noqa: BLE001
            return ProductPageState(status=ReadStatus.ERROR, url=self.current_url(), error=str(exc))

    def select_variant(self, variant: str) -> ActionResult:
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        try:
            root = _find_first(self._driver, self._profile.product_root) or self._driver
            select_element = _find_first(root, self._profile.product_variant_select)
            if select_element is not None:
                from selenium.webdriver.support.ui import Select

                selector = Select(select_element)
                for index, option in enumerate(selector.options):
                    if option.text.strip() == variant or " ".join(option.text.split()).casefold() == " ".join(variant.split()).casefold():
                        selector.select_by_index(index)
                        break
                else:
                    return ActionResult(False, f"商品页没有规格：{variant}", attempted=False)
            else:
                matches = [
                    element
                    for element in _find_all(root, self._profile.product_variant_option)
                    if _text(element).strip() == variant
                ]
                if len(matches) != 1 or not _enabled(matches[0]):
                    return ActionResult(False, f"商品规格定位不唯一或不可用：{variant}", attempted=False)
                try:
                    matches[0].click()
                except Exception:  # noqa: BLE001 - verified non-purchase control
                    if not self._source_ok().ok or not _enabled(matches[0]):
                        return ActionResult(False, "商品规格来源或状态已变化")
                    _js_click(self._driver, matches[0])
            page = self.read_product_page()
            if page.status is not ReadStatus.OK:
                return ActionResult(False, page.error or "规格选择后无法读取商品页")
            if (page.selected_variant or "").strip() != variant.strip():
                return ActionResult(False, f"规格未设置成功，当前为 {page.selected_variant or '未知'}", attempted=True)
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def set_quantity(self, quantity: int) -> ActionResult:
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        try:
            root = _find_first(self._driver, self._profile.product_root) or self._driver
            field = _find_first(root, self._profile.product_quantity_input)
            if not _enabled(field) or _attr(field, "readonly") is not None:
                return ActionResult(False, "未找到可用的商品数量控件", attempted=False)
            try:
                field.clear()
                field.send_keys(str(quantity))
                self._driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    field,
                )
            except Exception:  # noqa: BLE001 - verified non-purchase field fallback
                if not self._source_ok().ok or not _enabled(field) or _attr(field, "readonly") is not None:
                    return ActionResult(False, "商品数量控件来源或状态已变化")
                self._driver.execute_script(
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    field,
                    str(quantity),
                )
            page = self.read_product_page()
            if page.status is not ReadStatus.OK:
                return ActionResult(False, page.error or "数量设置后无法读取商品页")
            if page.quantity != quantity:
                return ActionResult(False, f"数量未设置成功，当前为 {page.quantity}", attempted=True)
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def add_to_cart(self, *, check: Callable[[], None] = lambda: None) -> ActionResult:
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        page = self.read_product_page()
        if page.status is not ReadStatus.OK:
            return ActionResult(False, page.error or "商品页无法读取", attempted=False)
        if not page.add_to_cart_enabled:
            return ActionResult(False, "加购按钮当前不可用", attempted=False)
        button = _find_first(self._driver, self._profile.product_add_button)
        if button is None:
            return ActionResult(False, "未找到加购按钮", attempted=False)
        check()
        try:
            before_url = self.current_url()
            button.click()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    if self.current_url() != before_url:
                        break
                except Exception:  # noqa: BLE001
                    break
                time.sleep(0.05)
            error = _text(_find_first(self._driver, self._profile.product_add_error))
            if error:
                return ActionResult(False, "官网报告加购异常，需核对购物车", attempted=True, uncertain=True)
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001 - click may have reached the site
            return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def read_product(self, url: str | None = None) -> ProductRead:
        page = self.open_product(url) if url is not None else self.read_product_page()
        return ProductRead(
            status=page.status, title=page.title, variants=page.variants, url=page.url, error=page.error,
        )

    def search_products(self, keyword: str) -> ProductSearchRead:
        keyword = keyword.strip()
        if not keyword:
            return ProductSearchRead(status=ReadStatus.ERROR, error="搜索关键词不能为空")
        try:
            deadline = time.monotonic() + self._page_load_timeout
            self.navigate(self._profile.home_url)
            field = _find_first(self._driver, self._profile.search_input)
            if field is None:
                toggle = _find_first(self._driver, self._profile.search_toggle)
                if toggle is not None and self._source_ok().ok and toggle.is_enabled():
                    toggle.click()
            while field is None and time.monotonic() < deadline and self._source_ok().ok:
                if _find_first(self._driver, self._profile.blocked_selector):
                    return ProductSearchRead(status=ReadStatus.BLOCKED, error="官网搜索访问受限")
                field = _find_first(self._driver, self._profile.search_input)
                if field is None:
                    time.sleep(0.1)
            if not self._source_ok().ok:
                return ProductSearchRead(status=ReadStatus.BLOCKED, error="官网搜索来源不可信")
            if field is None:
                return ProductSearchRead(status=ReadStatus.UNRECOGNIZED, error="未找到首页搜索输入框")
            form_action = self._driver.execute_script("return arguments[0].form?.action ?? null", field)
            if form_action and not self._policy.check_page_url(form_action).ok:
                return ProductSearchRead(status=ReadStatus.BLOCKED, error="官网搜索表单来源不可信")
            from selenium.webdriver.common.keys import Keys

            field.clear()
            field.send_keys(keyword, Keys.ENTER)
            filter_attempted = False
            while time.monotonic() < deadline:
                if not self._source_ok().ok or _find_first(self._driver, self._profile.blocked_selector):
                    return ProductSearchRead(status=ReadStatus.BLOCKED, error="官网搜索结果来源不可信或访问受限")
                root = _find_first(self._driver, self._profile.search_root)
                if root is not None:
                    stock = _find_first(self._driver, self._profile.search_stock)
                    if stock is not None and not (stock.is_selected() or _attr(stock, "aria-pressed") == "true"):
                        if not filter_attempted and _enabled(stock):
                            stock.click()
                            filter_attempted = True
                        time.sleep(0.1)
                        continue
                    candidates: list[ProductCandidate] = []
                    for result in _find_all(root, self._profile.search_result):
                        title = _text(_find_first(result, self._profile.search_result_title))
                        link = result if self._profile.search_result_is_link else _find_first(
                            result, self._profile.search_result_link,
                        )
                        href = _attr(link, "href")
                        title = title or _text(link)
                        if not title or not href or not self._policy.check_page_url(href).ok:
                            continue
                        variants = tuple(
                            text for element in _find_all(result, self._profile.product_variant_option)
                            if (text := _text(element))
                        )
                        candidate = ProductCandidate(title=title, url=href, variants=variants)
                        if candidate not in candidates:
                            candidates.append(candidate)
                    if candidates:
                        return ProductSearchRead(status=ReadStatus.OK, candidates=tuple(candidates[:20]))
                    if any(
                        re.search(r"no (?:matching |search )?(?:products|results)", _text(node), re.I)
                        for node in _find_all(root, self._profile.search_empty)
                    ):
                        return ProductSearchRead(status=ReadStatus.OK)
                time.sleep(0.15)
            return ProductSearchRead(status=ReadStatus.UNRECOGNIZED, error="未能核实官网搜索结果")
        except Exception:
            return ProductSearchRead(status=ReadStatus.ERROR, error="官网首页搜索交互失败")

    def read_cart(self) -> CartRead:
        source = self._source_ok()
        if not source.ok:
            return CartRead(status=ReadStatus.BLOCKED, page_url=self.current_url(), error=source.reason)
        try:
            page = self._driver
            if _find_first(page, self._profile.blocked_selector):
                return CartRead(status=ReadStatus.BLOCKED, page_url=self.current_url())
            if self.page_kind() is PageKind.LOGIN:
                return CartRead(status=ReadStatus.NOT_LOGGED_IN, page_url=self.current_url())
            logged_in = self.is_logged_in()
            if logged_in is False:
                return CartRead(status=ReadStatus.NOT_LOGGED_IN, page_url=self.current_url())
            root = _find_first(page, self._profile.cart_root)
            path = urlsplit(self.current_url()).path.casefold()
            on_cart = root is not None and (
                self._profile.cart_path_hint in path or _attr(root, "data-page") == "cart"
            )
            if not on_cart:
                return CartRead(
                    status=ReadStatus.NOT_AVAILABLE,
                    logged_in=logged_in,
                    page_url=self.current_url(),
                )
            root = root or page
            line_elements = _find_all(root, self._profile.cart_line)
            if not line_elements:
                empty = any(
                    _attr(node, "data-cart-empty") == "true"
                    or _text(node).casefold() in {"your cart's empty", "your cart is empty", "your cart is empty."}
                    for node in _find_all(root, self._profile.cart_empty)
                )
                return CartRead(
                    status=ReadStatus.EMPTY if empty else ReadStatus.UNRECOGNIZED,
                    lines=(),
                    currency=None,
                    logged_in=logged_in,
                    page_url=self.current_url(),
                )
            lines = tuple(line for element in line_elements if (line := _read_line(element, self._profile)) is not None)
            if len(lines) != len(line_elements):
                return CartRead(status=ReadStatus.UNRECOGNIZED, logged_in=logged_in, page_url=self.current_url())
            currency_text = _text(_find_first(root, self._profile.currency_selector))
            return CartRead(
                status=ReadStatus.OK,
                lines=lines,
                currency=detect_currency(currency_text or " ".join(
                    _text(_find_first(element, self._profile.line_unit_price)) for element in line_elements
                )),
                logged_in=logged_in,
                page_url=self.current_url(),
            )
        except Exception as exc:  # noqa: BLE001 - page failures become bounded read errors
            return CartRead(status=ReadStatus.ERROR, page_url=self.current_url(), error=str(exc))

    def _read_shipping_options(self, root) -> tuple[ShippingOption, ...]:
        options: list[ShippingOption] = []
        for element in _find_all(root, self._profile.shipping_option):
            input_element = _find_first(element, self._profile.shipping_option_input)
            selected = bool(input_element is not None and _attr(input_element, "checked") in ("true", "checked"))
            name = _text(_find_first(element, self._profile.shipping_option_name))
            amount_text = _text(_find_first(element, self._profile.shipping_option_amount))
            amount = _read_usd_amount(amount_text, allow_free=True)
            if not name:
                continue
            options.append(
                ShippingOption(
                    service_id=_attr(input_element, "value") or name,
                    name=name,
                    amount_usd=amount,
                    selected=selected,
                )
            )
        return tuple(options)

    def read_checkout(self) -> CheckoutRead:
        source = self._source_ok()
        if not source.ok:
            return CheckoutRead(status=ReadStatus.BLOCKED, page_url=self.current_url(), error=source.reason)
        try:
            page = self._driver
            if _find_first(page, self._profile.blocked_selector):
                return CheckoutRead(status=ReadStatus.BLOCKED, page_url=self.current_url())
            if self.page_kind() is PageKind.LOGIN:
                return CheckoutRead(status=ReadStatus.NOT_LOGGED_IN, page_url=self.current_url())
            root = _find_first(page, self._profile.checkout_root)
            if root is None:
                return CheckoutRead(status=ReadStatus.NOT_AVAILABLE, page_url=self.current_url())
            line_elements = _find_all(root, self._profile.checkout_line)
            lines = tuple(line for element in line_elements if (line := _read_line(element, self._profile)) is not None)
            if len(lines) != len(line_elements):
                return CheckoutRead(status=ReadStatus.UNRECOGNIZED, page_url=self.current_url())
            currency = detect_currency(
                _text(_find_first(root, self._profile.currency_selector))
                or _text(_find_first(root, self._profile.total_selector))
                or " ".join(_text(_find_first(line, self._profile.line_unit_price)) for line in line_elements)
            )
            form = FormState(contact_complete=False, card_complete=False)
            if self._filled_checkout is not None:
                flow, customer, card = self._filled_checkout
                try:
                    self._checkout_fields(flow, customer, card, write=False, check=lambda: None)
                    form = FormState(contact_complete=True, card_complete=True)
                except _CheckoutFieldError as exc:
                    form = FormState(contact_complete=False, card_complete=False, errors=(str(exc),))
            submit = _find_first(root, self._profile.final_submit_button)
            return CheckoutRead(
                status=ReadStatus.OK,
                lines=lines,
                currency=currency,
                subtotal_usd=_read_usd_amount(_text(_find_first(root, self._profile.subtotal_selector))),
                tax_usd=_read_usd_amount(_text(_find_first(root, self._profile.tax_selector))),
                shipping_total_usd=_read_usd_amount(_text(_find_first(root, self._profile.shipping_total_selector)), allow_free=True),
                total_usd=_read_usd_amount(_text(_find_first(root, self._profile.total_selector))),
                shipping_options=self._read_shipping_options(root),
                form=form,
                final_submit_enabled=_enabled(submit),
                page_url=self.current_url(),
            )
        except Exception as exc:  # noqa: BLE001
            return CheckoutRead(status=ReadStatus.ERROR, page_url=self.current_url(), error=str(exc))

    def enter_checkout(self) -> ActionResult:
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        button = _find_first(self._driver, self._profile.cart_checkout_button)
        if not _enabled(button):
            return ActionResult(False, "未找到可用的官方结算入口")
        try:
            button.click()
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001 - verified non-purchase navigation control
            try:
                if not self._source_ok().ok or not _enabled(button):
                    return ActionResult(False, "结算入口来源或状态已变化", uncertain=True, attempted=True)
                _js_click(self._driver, button)
                return ActionResult(True, attempted=True)
            except Exception:
                return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def _checkout_root(self, flow: tuple[str, str]):
        if not self._source_ok().ok:
            raise _CheckoutFieldError("结算顶层来源不可信")
        root = _find_first(self._driver, self._profile.checkout_root)
        if root is None or (self.current_url(), root.id) != flow:
            raise _CheckoutFieldError("结算页面已变化，无法确认同一结算流程")
        return root

    def _frame_source(self) -> None:
        url, origin = self._driver.execute_script("return [location.href, window.origin];")
        if url == "about:blank":
            raise _CheckoutFieldPending("信用卡 iframe 正在加载")
        source = self._policy.check_payment_url(url)
        if not source.ok or source.origin != origin:
            raise _CheckoutFieldError("信用卡 iframe 实际来源不可信")

    def _field(self, root, selector: str, name: str, value: str, *, write: bool, guard) -> None:
        fields = _find_all(root, selector)
        if not fields:
            raise _CheckoutFieldPending(f"缺少结算字段：{name}")
        if len(fields) != 1:
            raise _CheckoutFieldError(f"结算字段定位不唯一：{name}")
        field = fields[0]

        def accepted() -> bool:
            guard()
            valid, actual = self._driver.execute_script(
                """const el = arguments[0];
                   return [el.validity?.valid === true && el.getAttribute('aria-invalid') !== 'true', el.value];""",
                field,
            )
            if name in {"卡号", "有效期", "安全码", "电话"}:
                actual_digits = re.sub(r"\D", "", actual or "")
                expected_digits = re.sub(r"\D", "", value)
                if name == "有效期" and len(expected_digits) == 6:
                    return valid and actual_digits in {expected_digits, expected_digits[:2] + expected_digits[-2:]}
                return valid and actual_digits == expected_digits
            return valid and normalize_text(actual or "") == normalize_text(value)

        if accepted():
            return
        if not write:
            raise _CheckoutFieldPending(f"结算字段值或校验状态已变化：{name}")
        if not _enabled(field) or _attr(field, "readonly") is not None:
            raise _CheckoutFieldPending(f"结算字段暂不可编辑：{name}")
        if any(ord(char) < 32 or "\ue000" <= char <= "\uf8ff" for char in value):
            raise _CheckoutFieldError(f"结算字段包含控制字符：{name}")
        from selenium.common.exceptions import ElementNotInteractableException, InvalidElementStateException
        from selenium.webdriver.support.ui import Select

        guard()
        try:
            if field.tag_name == "select":
                Select(field).select_by_value(value)
            else:
                field.clear()
                guard()
                field.send_keys(value)
        except (ElementNotInteractableException, InvalidElementStateException):
            pass
        if not accepted():
            guard()
            self._driver.execute_script(
                """const [el, value] = arguments;
                   const prototype = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
                       : el instanceof HTMLInputElement ? HTMLInputElement.prototype : null;
                   if (!prototype || el.matches(':disabled,[readonly],[type=hidden],[type=submit],[type=radio],[type=checkbox]'))
                       return;
                   Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, value);
                   el.dispatchEvent(new Event('input', {bubbles: true}));
                   el.dispatchEvent(new Event('change', {bubbles: true}));
                   el.blur();""",
                field, value,
            )
            if not accepted():
                raise _CheckoutFieldPending(f"官网未接受结算字段：{name}")

    def _checkout_fields(self, flow, customer: Customer, card: Card, *, write: bool, check) -> None:
        profile = self._profile

        def top_guard():
            check()
            return self._checkout_root(flow)

        root = top_guard()
        contact = list(zip(
            profile.contact_fields,
            ("姓名", "邮箱", "电话", "地址", "城市", "州", "邮编"),
            (customer.full_name, customer.email, customer.phone, customer.address_line1,
             customer.city, customer.region, customer.postal_code),
            strict=True,
        ))
        if not _find_all(root, profile.contact_fields[0]):
            names = customer.full_name.split(maxsplit=1)
            if len(names) != 2:
                raise _CheckoutFieldError("结算要求分别填写名字和姓氏")
            contact[:1] = list(zip(profile.full_name_fields, ("名字", "姓氏"), names, strict=True))
        contact.insert(0, (profile.country_field, "国家", customer.country))
        if customer.address_line2 or _find_all(root, profile.address2_field):
            contact.append((profile.address2_field, "地址补充", customer.address_line2))
        # Set country before the state field, which can be rebuilt when country changes.
        for selector, name, value in contact:
            self._field(top_guard(), selector, name, value, write=write, guard=top_guard)

        card_values = (card.number, f"{card.expiry_month:02d}/{card.expiry_year}", card.cvv, card.holder_name)
        card_names = ("卡号", "有效期", "安全码", "持卡人")
        selectors = (*profile.card_fields, profile.card_holder_field)
        root = top_guard()
        frames_present = bool(self._driver.find_elements("css selector", profile.payment_iframe))
        if frames_present:
            self._hosted_checkout_url = flow[0]
        hosted = self._hosted_checkout_url == flow[0]
        for index, (selector, name, value) in enumerate(zip(selectors, card_names, card_values, strict=True)):
            root = top_guard()
            frames = self._driver.find_elements("css selector", profile.card_frames[index])
            if len(frames) > 1:
                raise _CheckoutFieldError(f"信用卡 iframe 定位不唯一：{name}")
            if frames:
                self._hosted_checkout_url = flow[0]
                hosted = True
                try:
                    self._driver.switch_to.frame(frames[0])

                    def frame_guard():
                        check()
                        if self.current_url() != flow[0] or not self._source_ok().ok:
                            raise _CheckoutFieldError("填写信用卡期间结算来源已变化")
                        self._frame_source()

                    frame_guard()
                    self._field(self._driver, selector, name, value, write=write, guard=frame_guard)
                finally:
                    self._driver.switch_to.default_content()
            elif hosted and index < len(profile.card_fields):
                raise _CheckoutFieldPending(f"缺少信用卡 iframe：{name}")
            else:
                def payment_guard():
                    top_guard()
                    if not self._policy.check_payment_url(self.current_url()).ok:
                        raise _CheckoutFieldError("主页面卡字段来源不可信")
                    if index < len(profile.card_fields) and (
                        self._hosted_checkout_url == flow[0]
                        or self._driver.find_elements("css selector", profile.payment_iframe)
                    ):
                        raise _CheckoutFieldPending(f"支付布局已切换至 iframe：{name}")

                payment_root = _find_first(root, profile.payment_root) if profile.payment_root else root
                if payment_root is None:
                    raise _CheckoutFieldPending(f"缺少结算付款区域：{name}")
                self._field(payment_root, selector, name, value, write=write, guard=payment_guard)

    def fill_checkout(
        self,
        customer: Customer,
        card: Card,
        policy: TrustedSourcePolicy,
        *,
        check: Callable[[], None] = lambda: None,
    ) -> ActionResult:
        from selenium.common.exceptions import (
            NoSuchElementException, NoSuchFrameException, StaleElementReferenceException, WebDriverException,
        )

        if policy != self._policy or not self._source_ok().ok:
            return ActionResult(False, "结算表单来源不可信")
        root = _find_first(self._driver, self._profile.checkout_root)
        if root is None:
            return ActionResult(False, "缺少官方结算表单")
        flow = (self.current_url(), root.id)
        self._filled_checkout = None
        deadline = time.monotonic() + self._page_load_timeout
        reason = "结算表单填写超时"

        def guard():
            if time.monotonic() >= deadline:
                raise _CheckoutFieldError(reason)
            check()

        try:
            while True:
                guard()
                try:
                    self._checkout_fields(flow, customer, card, write=True, check=guard)
                    self._checkout_fields(flow, customer, card, write=False, check=guard)
                    self._filled_checkout = (flow, customer, card)
                    return ActionResult(True, attempted=True)
                except _CheckoutFieldPending as exc:
                    reason = str(exc)
                except (StaleElementReferenceException, NoSuchFrameException, NoSuchElementException):
                    reason = "信用卡 iframe 或字段重建后未能及时核实"
                time.sleep(0.1)
        except _CheckoutFieldError as exc:
            return ActionResult(False, str(exc), attempted=True)
        except WebDriverException:
            return ActionResult(False, "浏览器无法继续核实结算表单", attempted=True)
        finally:
            self._driver.switch_to.default_content()

    def submit_purchase(self, *, check: Callable[[], None] = lambda: None) -> SubmitActionResult:
        source = self._source_ok()
        if not source.ok:
            return SubmitActionResult(attempted=False, sent=False, error=source.reason or "来源不可信")
        if self._filled_checkout is None:
            return SubmitActionResult(attempted=False, sent=False, error="付款表单未完成填写和回读")
        flow, customer, card = self._filled_checkout
        try:
            self._checkout_fields(flow, customer, card, write=False, check=lambda: None)
            root = self._checkout_root(flow)
        except _CheckoutFieldError as exc:
            return SubmitActionResult(attempted=False, sent=False, error=str(exc))
        buttons = _find_all(root, self._profile.final_submit_button)
        if len(buttons) != 1 or not _enabled(buttons[0]):
            return SubmitActionResult(attempted=False, sent=False, error="最终购买按钮不可用或定位不唯一")
        action = self._driver.execute_script(
            "const b = arguments[0]; return b.hasAttribute('formaction') ? b.formAction : b.form?.action;",
            buttons[0],
        )
        if action and not self._policy.check_page_url(action).ok:
            return SubmitActionResult(attempted=False, sent=False, error="最终购买表单提交来源不可信")
        check()
        if not self._source_ok().ok or self.current_url() != flow[0]:
            return SubmitActionResult(attempted=False, sent=False, error="付款前结算来源或页面已变化")
        self._filled_checkout = None
        try:
            buttons[0].click()
            return SubmitActionResult(attempted=True, sent=True)
        except Exception:
            return SubmitActionResult(attempted=True, sent=None, error="最终购买点击结果不明")

    def read_order_result(self) -> OrderResult:
        try:
            if not self._source_ok().ok:
                return OrderResult(status=OrderStatus.VERIFICATION, evidence="外部页面，等待本人核对并返回官网")
            page = self._driver
            if _find_first(page, self._profile.bank_challenge):
                return OrderResult(status=OrderStatus.VERIFICATION, evidence="请在专用 Chrome 中完成银行验证")
            if _find_first(page, self._profile.declined_selector):
                return OrderResult(status=OrderStatus.DECLINED, evidence="官网显示付款被拒绝")
            if _find_first(page, self._profile.processing_selector):
                return OrderResult(status=OrderStatus.PENDING, evidence="官网仍在处理")
            root = _find_first(page, self._profile.order_root)
            if root is None:
                return OrderResult(status=OrderStatus.UNKNOWN, evidence="未找到订单确认页面")
            order_id = _text(_find_first(root, self._profile.order_number)) or None
            payment_state = _text(_find_first(root, self._profile.payment_status)) or None
            order_lines = tuple(
                line
                for element in _find_all(root, self._profile.order_lines)
                if (line := _read_line(element, self._profile)) is not None
            )
            if order_id and payment_state and order_lines:
                return OrderResult(
                    status=OrderStatus.CONFIRMED,
                    order_id=order_id,
                    payment_state=payment_state,
                    evidence=f"订单 {order_id}，付款状态 {payment_state}",
                    matches_target=False,
                    lines=order_lines,
                )
            return OrderResult(
                status=OrderStatus.UNKNOWN,
                order_id=order_id,
                payment_state=payment_state,
                lines=order_lines,
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(status=OrderStatus.UNKNOWN, evidence=str(exc))

    def refresh(self) -> None:
        self._driver.refresh()

    def close(self) -> None:
        try:
            self._driver.quit()
        except Exception:  # noqa: BLE001
            pass


def build_chrome_options(chrome_path: str, profile_dir: Path, *, headless: bool = False):
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.binary_location = chrome_path
    options.add_argument(f"--user-data-dir={Path(profile_dir)}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if headless:
        options.add_argument("--headless=new")
    return options


class _SandboxedUCOptions:
    """Create UC options while preventing UC's unconditional --no-sandbox."""

    def __new__(cls, *args, **kwargs):
        import undetected_chromedriver as uc

        class Options(uc.ChromeOptions):
            def add_argument(self, *args, **kwargs):
                if args and args[0] == "--no-sandbox":
                    return self
                return super().add_argument(*args, **kwargs)

        return Options(*args, **kwargs)


def open_live_session(
    *,
    chrome_path: str,
    driver_path: str,
    profile_dir: Path,
    policy: TrustedSourcePolicy,
    profile: PageProfile = DEFAULT_TOPPs_PROFILE,
    page_load_timeout: float = 30.0,
    headless: bool = False,
    use_uc: bool = True,
    no_sandbox: bool = False,
) -> SeleniumPageAdapter:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass
    if use_uc:
        import undetected_chromedriver as uc

        from .driver_setup import inspect_chrome

        if not driver_path or not uc.Patcher(executable_path=driver_path).is_binary_patched():
            raise RuntimeError("指定驱动未完成 UC 暂存处理，请重新准备驱动")
        options = uc.ChromeOptions() if no_sandbox else _SandboxedUCOptions()
        options.binary_location = chrome_path
        options.add_argument(f"--user-data-dir={Path(profile_dir)}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--start-maximized")
        options.page_load_strategy = "normal"
        driver = uc.Chrome(
            options=options,
            driver_executable_path=driver_path,
            version_main=inspect_chrome(Path(chrome_path)).major_version,
            headless=headless,
            use_subprocess=True,
            no_sandbox=no_sandbox,
        )
    else:
        options = build_chrome_options(chrome_path, profile_dir, headless=headless)
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)
    driver.command_executor.client_config.timeout = page_load_timeout + 2
    driver.set_page_load_timeout(page_load_timeout)
    driver.set_script_timeout(page_load_timeout)
    return SeleniumPageAdapter(driver, profile, policy, page_load_timeout=page_load_timeout)


def verify_blank_browser(
    *,
    chrome_path: str,
    driver_path: str,
    timeout: float = 30.0,
    use_uc: bool = True,
    no_sandbox: bool = False,
):
    import shutil
    import tempfile

    profile_dir = Path(tempfile.mkdtemp(prefix="topps-driver-verify-"))
    try:
        adapter = open_live_session(
            chrome_path=chrome_path,
            driver_path=driver_path,
            profile_dir=profile_dir,
            policy=TrustedSourcePolicy(),
            page_load_timeout=timeout,
            headless=False,
            use_uc=use_uc,
            no_sandbox=no_sandbox,
        )
        try:
            adapter._driver.get("about:blank")
            if adapter.current_url() != "about:blank":
                raise RuntimeError("空白浏览器验证失败：无法打开 about:blank")
        finally:
            adapter.close()
    finally:
        # UC requests Chrome shutdown before its subprocess has finished writing the profile.
        cleanup_deadline = time.monotonic() + 3
        while profile_dir.exists():
            try:
                shutil.rmtree(profile_dir)
            except OSError:
                if time.monotonic() >= cleanup_deadline:
                    raise RuntimeError("UC 空白浏览器退出后未能清理临时 profile") from None
                time.sleep(0.15)
