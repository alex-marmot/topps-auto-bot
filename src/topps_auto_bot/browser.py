from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol
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
    allowed_payment_origins: frozenset[str] = frozenset()
    allowed_login_origins: frozenset[str] = frozenset()
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
    verified: bool
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
    payment_iframe: str = "iframe[data-payment-frame]"
    order_root: str = "[data-page='order-confirmation']"
    order_number: str = "[data-field='order-number']"
    payment_status: str = "[data-field='payment-status']"
    declined_selector: str = "[data-state='declined']"
    processing_selector: str = "[data-state='processing']"
    order_lines: str = "[data-testid='order-line']"
    product_root: str = "[data-page='product']"
    product_title: str = "[data-field='product-title']"
    product_variant_option: str = "[data-field='product-variant']"
    product_variant_select: str = "select[data-field='variant-select']"
    product_quantity_input: str = "input[data-field='quantity-input']"
    product_unit_price: str = "[data-field='product-price']"
    product_add_button: str = "[data-action='add-to-cart']"
    product_sold_out: str = "[data-state='sold-out']"
    product_add_error: str = "[data-add-error]"
    cart_url: str = "https://www.topps.com/checkout/cart"
    search_url_template: str = "https://www.topps.com/search?q={query}"
    search_root: str = "[data-page='search']"
    search_result: str = "[data-testid='search-result']"
    search_result_title: str = "[data-field='result-title']"
    search_result_link: str = "a[href]"


DEFAULT_TOPPs_PROFILE = PageProfile(
    name="topps-unverified",
    verified=False,
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

    def add_to_cart(self) -> ActionResult: ...

    def read_cart(self) -> CartRead: ...

    def read_checkout(self) -> CheckoutRead: ...

    def enter_checkout(self) -> ActionResult: ...

    def fill_checkout(
        self,
        customer: Customer,
        card: Card,
        policy: TrustedSourcePolicy,
    ) -> ActionResult: ...

    def submit_purchase(self) -> SubmitActionResult: ...

    def read_order_result(self) -> OrderResult: ...

    def refresh(self) -> None: ...

    def close(self) -> None: ...


_MONEY = re.compile(r"(?<![\d.,])(?:USD|\$)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)(?![0-9.])", re.IGNORECASE)


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
    if "USD" in upper or "$" in upper:
        return USD
    match = re.search(r"\b([A-Z]{3})\b", upper)
    return match.group(1) if match else None


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


def _js_click(driver, element) -> None:
    driver.execute_script("arguments[0].click();", element)


def _read_line(line_element, profile: PageProfile) -> OrderLine | None:
    title = _text(_find_first(line_element, profile.line_title))
    variant = _text(_find_first(line_element, profile.line_variant))
    quantity = parse_display_quantity(_text(_find_first(line_element, profile.line_quantity)))
    price = parse_display_money(_text(_find_first(line_element, profile.line_unit_price)))
    if not title or quantity is None:
        return None
    return OrderLine(
        title=title,
        variant=variant,
        quantity=quantity,
        unit_price_usd=price,
    )


class SeleniumPageAdapter:
    """Selenium adapter with an explicitly verified page profile.

    A profile is only usable after live-site validation. Until then read/action
    methods report the page as unrecognized instead of guessing selectors.
    """

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

    def current_url(self) -> str:
        return self._driver.current_url

    def navigate(self, url: str) -> None:
        source = self._policy.check_page_url(url)
        if not source.ok:
            raise ValueError(source.reason or "来源不可信")
        self._driver.get(url)

    def _source_ok(self) -> SourceCheck:
        return self._policy.check_page_url(self.current_url())

    def _login_source_ok(self) -> SourceCheck:
        page = self._policy.check_page_url(self.current_url())
        if page.ok:
            return page
        return self._policy.check_login_url(self.current_url())

    def _unverified(self) -> bool:
        return not self._profile.verified

    def page_kind(self) -> PageKind:
        if self._unverified():
            return PageKind.OTHER
        url = self.current_url().casefold()
        if self._profile.blocked_selector and _find_first(self._driver, self._profile.blocked_selector):
            return PageKind.BLOCKED
        if _find_first(self._driver, self._profile.checkout_root):
            return PageKind.CHECKOUT
        if _find_first(self._driver, self._profile.cart_root) or self._profile.cart_path_hint in url:
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
        if self._unverified():
            return None
        if self.page_kind() is PageKind.LOGIN:
            return False
        return _find_first(self._driver, self._profile.login_selector) is not None

    def account_identity(self) -> str | None:
        if self._unverified():
            return None
        element = _find_first(self._driver, self._profile.account_identity)
        if element is None:
            return None
        return _text(element) or _attr(element, "content") or None

    def read_login_state(self) -> LoginRead:
        if self._unverified():
            return LoginRead(status=LoginStatus.UNRECOGNIZED, error="官网登录页面定位尚未现场验证")
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
            login_root = _find_first(page, self._profile.login_page_root) or _find_first(
                page, self._profile.login_email_field
            )
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
        if self._unverified():
            return ActionResult(False, "官网登录页面定位尚未现场验证")
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
            root = _find_first(self._driver, self._profile.login_page_root) or self._driver
            email_field = _find_first(root, self._profile.login_email_field)
            if email_field is None:
                return ActionResult(False, "未找到已核实的登录账号输入框", attempted=False)
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
        if self._unverified():
            return ActionResult(False, "官网购物车页面定位尚未现场验证")
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
        if self._unverified():
            return ProductPageState(status=ReadStatus.UNRECOGNIZED, error="官网商品页面定位尚未现场验证")
        source = self._policy.check_page_url(url)
        if not source.ok:
            return ProductPageState(status=ReadStatus.BLOCKED, error=source.reason)
        try:
            self._driver.get(url)
            return self.read_product_page()
        except Exception as exc:  # noqa: BLE001
            return ProductPageState(status=ReadStatus.ERROR, error=str(exc))

    def read_product_page(self) -> ProductPageState:
        if self._unverified():
            return ProductPageState(status=ReadStatus.UNRECOGNIZED, error="官网商品页面定位尚未现场验证")
        source = self._source_ok()
        if not source.ok:
            return ProductPageState(status=ReadStatus.BLOCKED, url=self.current_url(), error=source.reason)
        try:
            page = self._driver
            if _find_first(page, self._profile.blocked_selector):
                return ProductPageState(status=ReadStatus.BLOCKED, url=self.current_url())
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
                selected_text = selector.first_selected_option.text.strip() if selector.options else ""
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
                currency=currency or ("USD" if price is not None else None),
                add_to_cart_enabled=enabled,
                single_variant=len(variants) <= 1,
            )
        except Exception as exc:  # noqa: BLE001
            return ProductPageState(status=ReadStatus.ERROR, url=self.current_url(), error=str(exc))

    def select_variant(self, variant: str) -> ActionResult:
        if self._unverified():
            return ActionResult(False, "官网商品页面定位尚未现场验证")
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
                if not matches:
                    return ActionResult(False, f"商品页没有规格：{variant}", attempted=False)
                try:
                    matches[0].click()
                except Exception:  # noqa: BLE001 - verified non-purchase control
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
        if self._unverified():
            return ActionResult(False, "官网商品页面定位尚未现场验证")
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        try:
            root = _find_first(self._driver, self._profile.product_root) or self._driver
            field = _find_first(root, self._profile.product_quantity_input)
            if field is None:
                return ActionResult(False, "未找到商品数量控件", attempted=False)
            try:
                field.clear()
                field.send_keys(str(quantity))
                self._driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    field,
                )
            except Exception:  # noqa: BLE001 - verified non-purchase field fallback
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

    def add_to_cart(self) -> ActionResult:
        if self._unverified():
            return ActionResult(False, "官网商品页面定位尚未现场验证")
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
            error = _text(_find_first(self._driver, self._profile.product_add_error)) or None
            if error:
                return ActionResult(False, error, attempted=True, confirmed_no_side_effect=True)
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001 - click may have reached the site
            return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def read_product(self, url: str | None = None) -> ProductRead:
        if self._unverified():
            return ProductRead(status=ReadStatus.UNRECOGNIZED, error="官网商品页面定位尚未现场验证")
        if url is not None:
            source = self._policy.check_page_url(url)
            if not source.ok:
                return ProductRead(status=ReadStatus.BLOCKED, error=source.reason)
            try:
                self._driver.get(url)
            except Exception as exc:  # noqa: BLE001
                return ProductRead(status=ReadStatus.ERROR, error=str(exc))
        source = self._source_ok()
        if not source.ok:
            return ProductRead(status=ReadStatus.BLOCKED, url=self.current_url(), error=source.reason)
        try:
            root = _find_first(self._driver, self._profile.product_root) or self._driver
            title = _text(_find_first(root, self._profile.product_title))
            variants = tuple(
                text
                for element in _find_all(root, self._profile.product_variant_option)
                if (text := _text(element))
            )
            if not title:
                return ProductRead(status=ReadStatus.UNRECOGNIZED, url=self.current_url())
            return ProductRead(
                status=ReadStatus.OK,
                title=title,
                variants=variants,
                url=self.current_url(),
            )
        except Exception as exc:  # noqa: BLE001
            return ProductRead(status=ReadStatus.ERROR, url=self.current_url(), error=str(exc))

    def search_products(self, keyword: str) -> ProductSearchRead:
        if self._unverified():
            return ProductSearchRead(status=ReadStatus.UNRECOGNIZED, error="官网搜索页面定位尚未现场验证")
        keyword = keyword.strip()
        if not keyword:
            return ProductSearchRead(status=ReadStatus.ERROR, error="搜索关键词不能为空")
        from urllib.parse import quote_plus, urljoin

        url = self._profile.search_url_template.format(query=quote_plus(keyword))
        source = self._policy.check_page_url(url)
        if not source.ok:
            return ProductSearchRead(status=ReadStatus.BLOCKED, error=source.reason)
        try:
            self._driver.get(url)
            final_source = self._source_ok()
            if not final_source.ok:
                return ProductSearchRead(status=ReadStatus.BLOCKED, error=final_source.reason)
            root = _find_first(self._driver, self._profile.search_root) or self._driver
            candidates: list[ProductCandidate] = []
            for result in _find_all(root, self._profile.search_result):
                title = _text(_find_first(result, self._profile.search_result_title))
                link = _find_first(result, self._profile.search_result_link)
                href = _attr(link, "href") if link is not None else None
                if not title or not href:
                    continue
                href = urljoin(self.current_url(), href)
                candidate_source = self._policy.check_page_url(href)
                if not candidate_source.ok:
                    continue
                variants = tuple(
                    text
                    for element in _find_all(result, self._profile.product_variant_option)
                    if (text := _text(element))
                )
                candidates.append(ProductCandidate(title=title, url=href, variants=variants))
            return ProductSearchRead(status=ReadStatus.OK, candidates=tuple(candidates[:20]))
        except Exception as exc:  # noqa: BLE001
            return ProductSearchRead(status=ReadStatus.ERROR, error=str(exc))

    def read_cart(self) -> CartRead:
        if self._unverified():
            return CartRead(status=ReadStatus.UNRECOGNIZED, error="官网页面定位尚未现场验证")
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
            on_cart = root is not None or self._profile.cart_path_hint in self.current_url().casefold()
            if not on_cart:
                return CartRead(
                    status=ReadStatus.NOT_AVAILABLE,
                    logged_in=logged_in,
                    page_url=self.current_url(),
                )
            root = root or page
            line_elements = _find_all(root, self._profile.cart_line)
            if not line_elements:
                return CartRead(
                    status=ReadStatus.EMPTY,
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
                currency=detect_currency(currency_text) or USD,
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
            amount = parse_display_money(amount_text)
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
        if self._unverified():
            return CheckoutRead(status=ReadStatus.UNRECOGNIZED, error="官网页面定位尚未现场验证")
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
                _text(_find_first(root, self._profile.currency_selector)) or "USD"
            ) or USD
            contact_ok = all(_text(_find_first(root, selector)) or _attr(_find_first(root, selector), "value") for selector in self._profile.contact_fields)
            card_ok = all(_find_first(root, selector) is not None for selector in self._profile.card_fields)
            form = FormState(contact_complete=bool(contact_ok), card_complete=bool(card_ok))
            submit = _find_first(root, self._profile.final_submit_button)
            payment_origin = None
            payment_frame = _find_first(page, self._profile.payment_iframe)
            if payment_frame is not None:
                src = _attr(payment_frame, "src") or ""
                payment_origin = self._policy._origin(urlsplit(src)) if src else None
            return CheckoutRead(
                status=ReadStatus.OK,
                lines=lines,
                currency=currency,
                subtotal_usd=parse_display_money(_text(_find_first(root, self._profile.subtotal_selector))),
                tax_usd=parse_display_money(_text(_find_first(root, self._profile.tax_selector))),
                shipping_total_usd=parse_display_money(_text(_find_first(root, self._profile.shipping_total_selector))),
                total_usd=parse_display_money(_text(_find_first(root, self._profile.total_selector))),
                shipping_options=self._read_shipping_options(root),
                form=form,
                final_submit_enabled=bool(submit is not None and _displayed(submit)),
                payment_origin=payment_origin,
                page_url=self.current_url(),
            )
        except Exception as exc:  # noqa: BLE001
            return CheckoutRead(status=ReadStatus.ERROR, page_url=self.current_url(), error=str(exc))

    def enter_checkout(self) -> ActionResult:
        if self._unverified():
            return ActionResult(False, "官网页面定位尚未现场验证")
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        button = _find_first(self._driver, self._profile.cart_checkout_button)
        if button is None:
            return ActionResult(False, "未找到可用的官方结算入口")
        try:
            button.click()
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001 - verified non-purchase navigation control
            try:
                _js_click(self._driver, button)
                return ActionResult(True, attempted=True)
            except Exception:
                return ActionResult(False, str(exc), uncertain=True, attempted=True)

    def fill_checkout(
        self,
        customer: Customer,
        card: Card,
        policy: TrustedSourcePolicy,
    ) -> ActionResult:
        if self._unverified():
            return ActionResult(False, "官网页面定位尚未现场验证")
        source = self._source_ok()
        if not source.ok:
            return ActionResult(False, source.reason or "来源不可信")
        checkout = self.read_checkout()
        if checkout.status is not ReadStatus.OK:
            return ActionResult(False, "结算页尚未就绪或无法读取")
        if checkout.payment_origin is not None:
            payment = policy.check_payment_url(checkout.payment_origin)
            if not payment.ok:
                return ActionResult(False, "支付页面来源未核实")
        try:
            root = _find_first(self._driver, self._profile.checkout_root) or self._driver
            values = {
                "[name='full_name']": customer.full_name,
                "[name='email']": customer.email,
                "[name='phone']": customer.phone,
                "[name='address1']": customer.address_line1,
                "[name='address2']": customer.address_line2,
                "[name='city']": customer.city,
                "[name='region']": customer.region,
                "[name='postal_code']": customer.postal_code,
                "[name='country']": customer.country,
                "[name='card_number']": card.number,
                "[name='expiry']": f"{card.expiry_month:02d}/{card.expiry_year}",
                "[name='cvv']": card.cvv,
                "[name='holder_name']": card.holder_name,
            }
            for selector, value in values.items():
                field = _find_first(root, selector)
                if field is None:
                    if selector in self._profile.contact_fields or selector in self._profile.card_fields:
                        return ActionResult(False, f"缺少官网字段：{selector}")
                    continue
                field.clear()
                field.send_keys(value)
            return ActionResult(True, attempted=True)
        except Exception as exc:  # noqa: BLE001 - never continue with a partial form
            return ActionResult(False, str(exc), attempted=True)

    def submit_purchase(self) -> SubmitActionResult:
        if self._unverified():
            return SubmitActionResult(attempted=False, sent=False, error="官网页面定位尚未现场验证")
        source = self._source_ok()
        if not source.ok:
            return SubmitActionResult(attempted=False, sent=False, error=source.reason or "来源不可信")
        button = _find_first(self._driver, self._profile.final_submit_button)
        if button is None or not _displayed(button):
            return SubmitActionResult(attempted=False, sent=False, error="最终购买按钮不可用")
        try:
            button.click()
            return SubmitActionResult(attempted=True, sent=True)
        except Exception as exc:  # noqa: BLE001 - click may have reached the server
            return SubmitActionResult(attempted=True, sent=None, error=str(exc))

    def read_order_result(self) -> OrderResult:
        if self._unverified():
            return OrderResult(status=OrderStatus.UNKNOWN, evidence="官网页面定位尚未现场验证")
        try:
            page = self._driver
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


def _chrome_major_version(chrome_path: str) -> int:
    import subprocess

    completed = subprocess.run(
        [chrome_path, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    match = re.search(r"(\d+)\.", completed.stdout + completed.stderr)
    if not match:
        raise RuntimeError("无法从 Chrome 读取主版本")
    return int(match.group(1))


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

        options = _SandboxedUCOptions()
        options.binary_location = chrome_path
        options.add_argument(f"--user-data-dir={Path(profile_dir)}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--start-maximized")
        options.page_load_strategy = "normal"
        driver = uc.Chrome(
            options=options,
            driver_executable_path=driver_path,
            version_main=_chrome_major_version(chrome_path),
            headless=headless,
            use_subprocess=True,
            no_sandbox=no_sandbox,
        )
    else:
        options = build_chrome_options(chrome_path, profile_dir, headless=headless)
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(page_load_timeout)
    return SeleniumPageAdapter(driver, profile, policy, page_load_timeout=page_load_timeout)


def verify_blank_browser(
    *,
    chrome_path: str,
    driver_path: str,
    timeout: float = 30.0,
    use_uc: bool = True,
    no_sandbox: bool = False,
):
    import tempfile

    with tempfile.TemporaryDirectory(prefix="topps-driver-verify-") as profile_dir:
        adapter = open_live_session(
            chrome_path=chrome_path,
            driver_path=driver_path,
            profile_dir=Path(profile_dir),
            policy=TrustedSourcePolicy(),
            page_load_timeout=timeout,
            headless=True,
            use_uc=use_uc,
            no_sandbox=no_sandbox,
        )
        try:
            adapter._driver.get("about:blank")
            if adapter.current_url() != "about:blank":
                raise RuntimeError("空白浏览器验证失败：无法打开 about:blank")
        finally:
            adapter.close()
