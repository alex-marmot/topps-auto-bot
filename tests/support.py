from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from topps_auto_bot.models import (
    ActionResult,
    ActiveWindow,
    AppConfig,
    Card,
    CartRead,
    CheckoutRead,
    Customer,
    DriverSettings,
    FormState,
    LoginCredentials,
    LoginRead,
    LoginStatus,
    OrderLine,
    OrderResult,
    OrderStatus,
    PageKind,
    ProductPageState,
    ProductPreset,
    ReadStatus,
    ScheduleMode,
    ScheduleSettings,
    ShippingOption,
    ShippingSnapshot,
    SubmitActionResult,
    TargetSnapshot,
    TaskSnapshot,
    UTC,
)

SYNTHETIC_CARD = "4111111111111111"
SYNTHETIC_CVV = "123"
CARD_MARKER = "4111111111111111"
ADDRESS_MARKER = "1 Synthetic Test Lane"
EMAIL_MARKER = "synthetic@example.test"
NAME_MARKER = "Synthetic Tester"
LOGIN_MARKER = "login-synthetic@example.test"
LOGIN_PASSWORD = "synthetic-login-password"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def make_preset(
    *,
    preset_id: str = "preset-a",
    title: str = "Synthetic Card",
    variant: str = "Hobby Box",
    quantity: int = 2,
    unit_price_limit: str = "25.00",
    url: str = "https://www.topps.com/products/synthetic-card",
) -> ProductPreset:
    return ProductPreset(
        preset_id=preset_id,
        title=title,
        variant=variant,
        url=url,
        quantity=quantity,
        unit_price_limit_usd=Decimal(unit_price_limit),
    )


def make_customer() -> Customer:
    return Customer(
        full_name=NAME_MARKER,
        email=EMAIL_MARKER,
        phone="555-0100",
        address_line1=ADDRESS_MARKER,
        address_line2="",
        city="Test City",
        region="TS",
        postal_code="00000",
        country="US",
    )


def make_login(
    *,
    email: str = LOGIN_MARKER,
    password: str = LOGIN_PASSWORD,
) -> LoginCredentials:
    return LoginCredentials(email=email, password=password)


def make_card() -> Card:
    return Card(
        number=SYNTHETIC_CARD,
        expiry_month=12,
        expiry_year=2030,
        cvv=SYNTHETIC_CVV,
        holder_name=NAME_MARKER,
    )


def make_config(
    *,
    presets: tuple[ProductPreset, ...] | None = None,
    selected: str | None = "preset-a",
    order_limit: str = "70.00",
    schedule: ScheduleSettings | None = None,
    driver: DriverSettings | None = None,
    login: LoginCredentials | None = None,
) -> AppConfig:
    presets = presets or (make_preset(),)
    return AppConfig(
        schema_version=2,
        presets=presets,
        selected_preset_id=selected,
        order_limit_usd=Decimal(order_limit),
        login=login or make_login(),
        customer=make_customer(),
        card=make_card(),
        driver=driver or DriverSettings(mode="automatic"),
        schedule=schedule or ScheduleSettings(mode=ScheduleMode.IMMEDIATE, window_seconds=60),
    )


def make_snapshot(
    *,
    config: AppConfig | None = None,
    task_id: str = "task-1",
    schedule: ScheduleSettings | None = None,
    now: datetime | None = None,
) -> TaskSnapshot:
    config = config or make_config()
    target = config.target_snapshot()
    assert target is not None
    return TaskSnapshot(
        task_id=task_id,
        target=target,
        login=config.login,
        customer=config.customer,
        card=config.card,
        driver=config.driver,
        schedule=schedule or config.schedule,
        created_at_utc=now or datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC),
    )


def make_line(
    *,
    title: str = "Synthetic Card",
    variant: str = "Hobby Box",
    quantity: int = 2,
    unit_price: str | None = "20.00",
) -> OrderLine:
    return OrderLine(
        title=title,
        variant=variant,
        quantity=quantity,
        unit_price_usd=Decimal(unit_price) if unit_price is not None else None,
    )


def make_cart(
    *,
    lines: tuple[OrderLine, ...] | None = None,
    currency: str | None = "USD",
    logged_in: bool | None = True,
    status: ReadStatus = ReadStatus.OK,
) -> CartRead:
    return CartRead(
        status=status,
        lines=lines if lines is not None else (make_line(),),
        currency=currency,
        logged_in=logged_in,
        page_url="https://www.topps.com/checkout/cart",
    )


def make_checkout(
    *,
    lines: tuple[OrderLine, ...] | None = None,
    currency: str | None = "USD",
    subtotal: str | None = "40.00",
    tax: str | None = "5.00",
    shipping_total: str | None = "10.00",
    total: str | None = "55.00",
    shipping_options: tuple[ShippingOption, ...] | None = None,
    form: FormState | None = None,
    final_submit_enabled: bool = True,
    payment_origin: str | None = None,
    status: ReadStatus = ReadStatus.OK,
) -> CheckoutRead:
    return CheckoutRead(
        status=status,
        lines=lines if lines is not None else (make_line(),),
        currency=currency,
        subtotal_usd=Decimal(subtotal) if subtotal is not None else None,
        tax_usd=Decimal(tax) if tax is not None else None,
        shipping_total_usd=Decimal(shipping_total) if shipping_total is not None else None,
        total_usd=Decimal(total) if total is not None else None,
        shipping_options=shipping_options
        if shipping_options is not None
        else (
            ShippingOption("standard", "Standard", Decimal("10.00"), True),
            ShippingOption("express", "Express", Decimal("25.00"), False),
        ),
        form=form or FormState(contact_complete=True, card_complete=True),
        final_submit_enabled=final_submit_enabled,
        payment_origin=payment_origin,
        page_url="https://www.topps.com/checkout",
    )


def make_product_page(
    *,
    title: str = "Synthetic Card",
    url: str = "https://www.topps.com/products/synthetic-card",
    variants: tuple[str, ...] = ("Hobby Box", "Retail Box"),
    selected_variant: str | None = "Hobby Box",
    quantity: int = 1,
    unit_price: str | None = "20.00",
    currency: str | None = "USD",
    add_to_cart_enabled: bool = True,
    single_variant: bool = False,
    status: ReadStatus = ReadStatus.OK,
) -> ProductPageState:
    return ProductPageState(
        status=status,
        title=title,
        url=url,
        variants=variants,
        selected_variant=selected_variant,
        quantity=quantity,
        unit_price_usd=Decimal(unit_price) if unit_price is not None else None,
        currency=currency,
        add_to_cart_enabled=add_to_cart_enabled,
        single_variant=single_variant,
    )


def empty_cart() -> CartRead:
    return CartRead(status=ReadStatus.EMPTY, lines=(), currency=None, logged_in=True, page_url="https://www.topps.com/checkout/cart")


def shipping_snapshot(amount: str = "10.00", service_id: str = "standard", name: str = "Standard") -> ShippingSnapshot:
    return ShippingSnapshot(service_id=service_id, name=name, amount_usd=Decimal(amount))


class FakeSession:
    def __init__(
        self,
        *,
        page: PageKind = PageKind.CART,
        cart: CartRead | None = None,
        checkout: CheckoutRead | None = None,
        logged_in: bool | None = True,
        order_results: list[OrderResult] | None = None,
        submit_result: SubmitActionResult | None = None,
        enter_result: ActionResult | None = None,
    ) -> None:
        self.page = page
        self.cart = cart if cart is not None else make_cart()
        self.checkout = checkout if checkout is not None else make_checkout()
        self.logged_in = logged_in
        self.order_results = list(order_results or [])
        self.submit_result = submit_result or SubmitActionResult(attempted=True, sent=True)
        self.enter_result = enter_result or ActionResult(True, attempted=True)
        self.cart_reads = 0
        self.checkout_reads = 0
        self.enter_calls = 0
        self.fill_calls = 0
        self.submit_calls = 0
        self.refresh_calls = 0
        self.closed = False
        self.on_cart_read: Callable[[FakeSession], None] | None = None
        self.on_checkout_read: Callable[[FakeSession], None] | None = None
        self.on_submit: Callable[[FakeSession], None] | None = None
        self.login_credentials = make_login()
        self.login_state = (
            LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity=self.login_credentials.email,
                page_url="https://www.topps.com/checkout/cart",
            )
            if logged_in is True
            else LoginRead(status=LoginStatus.NOT_LOGGED_IN, page_url="https://www.topps.com/checkout/cart")
        )
        self.login_calls = 0
        self.login_state_reads = 0
        self.login_attempts: list[LoginCredentials] = []
        self.on_login: Callable[[FakeSession], None] | None = None
        self.on_login_state_read: Callable[[FakeSession], None] | None = None

    def current_url(self) -> str:
        return "https://www.topps.com/cart" if self.page is PageKind.CART else "https://www.topps.com/checkout"

    def page_kind(self) -> PageKind:
        return self.page

    def is_logged_in(self) -> bool | None:
        return self.logged_in

    def read_login_state(self) -> LoginRead:
        self.login_state_reads += 1
        if self.on_login_state_read is not None:
            self.on_login_state_read(self)
        return self.login_state

    def login_with_credentials(self, login: LoginCredentials) -> ActionResult:
        self.login_calls += 1
        self.login_attempts.append(login)
        if self.on_login is not None:
            self.on_login(self)
        else:
            self.logged_in = True
            self.login_state = LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity=login.email,
                page_url="https://www.topps.com/checkout/cart",
            )
        return ActionResult(True, attempted=True)

    def read_cart(self) -> CartRead:
        self.cart_reads += 1
        if self.on_cart_read is not None:
            self.on_cart_read(self)
        return self.cart

    def read_checkout(self) -> CheckoutRead:
        self.checkout_reads += 1
        if self.on_checkout_read is not None:
            self.on_checkout_read(self)
        return self.checkout

    def enter_checkout(self) -> ActionResult:
        self.enter_calls += 1
        if self.enter_result.ok:
            self.page = PageKind.CHECKOUT
        return self.enter_result

    def fill_checkout(self, customer: Customer, card: Card, policy) -> ActionResult:
        self.fill_calls += 1
        if self.checkout.form is not None:
            self.checkout = replace(
                self.checkout,
                form=FormState(contact_complete=True, card_complete=True),
            )
        return ActionResult(True, attempted=True)

    def submit_purchase(self) -> SubmitActionResult:
        self.submit_calls += 1
        if self.on_submit is not None:
            self.on_submit(self)
        return self.submit_result

    def read_order_result(self) -> OrderResult:
        if self.order_results:
            return self.order_results.pop(0)
        return OrderResult(
            status=OrderStatus.CONFIRMED,
            order_id="ORDER-1",
            payment_state="paid",
            evidence="订单 ORDER-1 已确认",
            matches_target=True,
            lines=(make_line(),),
        )

    def refresh(self) -> None:
        self.refresh_calls += 1

    def close(self) -> None:
        self.closed = True


def window(
    *,
    start: datetime,
    seconds: int = 60,
    mode: ScheduleMode = ScheduleMode.IMMEDIATE,
    effective_start: datetime | None = None,
) -> ActiveWindow:
    return ActiveWindow(
        mode=mode,
        started_at_utc=start,
        effective_start_utc=effective_start or start,
        deadline_utc=start + timedelta(seconds=seconds),
    )


class FakeFullSession(FakeSession):
    def __init__(
        self,
        *,
        cart: CartRead | None = None,
        product: ProductPageState | None = None,
        logged_in: bool | None = True,
        checkout: CheckoutRead | None = None,
        order_results: list[OrderResult] | None = None,
        add_result: ActionResult | None = None,
        submit_result: SubmitActionResult | None = None,
    ) -> None:
        super().__init__(
            page=PageKind.CART,
            cart=cart if cart is not None else empty_cart(),
            checkout=checkout,
            logged_in=logged_in,
            order_results=order_results,
            submit_result=submit_result,
        )
        self.product = product if product is not None else make_product_page()
        self.add_result = add_result or ActionResult(True, attempted=True)
        self.product_reads = 0
        self.product_opens = 0
        self.select_calls = 0
        self.quantity_calls = 0
        self.add_calls = 0
        self.cart_navigations = 0
        self.login_result: ActionResult = ActionResult(True, attempted=True)
        self.on_add: Callable[[FakeFullSession], None] | None = None
        self.on_navigate_cart: Callable[[FakeFullSession], None] | None = None
        self.on_product_read: Callable[[FakeFullSession], None] | None = None

    def login_with_credentials(self, login: LoginCredentials) -> ActionResult:
        self.login_calls += 1
        self.login_attempts.append(login)
        if self.on_login is not None:
            self.on_login(self)
        else:
            self.logged_in = True
            self.login_state = LoginRead(
                status=LoginStatus.LOGGED_IN,
                account_identity=login.email,
                page_url="https://www.topps.com/checkout/cart",
            )
        return self.login_result

    def navigate_to_cart(self) -> ActionResult:
        self.cart_navigations += 1
        self.page = PageKind.CART
        if self.on_navigate_cart is not None:
            self.on_navigate_cart(self)
        return ActionResult(True, attempted=True)

    def open_product(self, url: str) -> ProductPageState:
        self.product_opens += 1
        self.page = PageKind.PRODUCT
        return self.product

    def read_product_page(self) -> ProductPageState:
        self.product_reads += 1
        if self.on_product_read is not None:
            self.on_product_read(self)
        return self.product

    def select_variant(self, variant: str) -> ActionResult:
        self.select_calls += 1
        if variant in self.product.variants:
            self.product = replace(self.product, selected_variant=variant)
            return ActionResult(True, attempted=True)
        return ActionResult(False, f"没有规格 {variant}", attempted=False)

    def set_quantity(self, quantity: int) -> ActionResult:
        self.quantity_calls += 1
        self.product = replace(self.product, quantity=quantity)
        return ActionResult(True, attempted=True)

    def add_to_cart(self) -> ActionResult:
        self.add_calls += 1
        if self.on_add is not None:
            self.on_add(self)
        return self.add_result

    def read_cart(self) -> CartRead:
        self.cart_reads += 1
        if self.page is not PageKind.CART:
            return CartRead(status=ReadStatus.NOT_AVAILABLE, page_url=self.current_url())
        return self.cart
