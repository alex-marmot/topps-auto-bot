from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import pytest

from tests.support import make_config
from topps_auto_bot.models import PreparedLevel, SubmissionState
from topps_auto_bot.presenter import SHIPPING_POLICY_TEXT
from topps_auto_bot.purchase import SubmissionStore, build_submission_record
from topps_auto_bot.settings import AppPaths, load_config
from topps_auto_bot.ui import ToppsAutoBotApp, configure_tcl_environment
from tests.support import shipping_snapshot
from datetime import datetime
from decimal import Decimal
from topps_auto_bot.models import UTC


def new_root() -> tk.Tk:
    configure_tcl_environment()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk 不可用：{exc}")
    root.withdraw()
    return root


def test_ui_first_run_save_and_restart(tmp_path: Path) -> None:
    paths = AppPaths.under(tmp_path / "data")
    root = new_root()
    try:
        app = ToppsAutoBotApp(root, paths=paths)
        assert "尚未配置" in app.status_var.get()
        app.preset_title_var.set("Synthetic Card")
        app.preset_variant_var.set("Hobby Box")
        app.preset_url_var.set("https://www.topps.com/products/synthetic-card")
        app.preset_quantity_var.set("2")
        app.preset_price_var.set("25.00")
        app._upsert_preset()
        app._select_preset()
        app.login_email_var.set("login-synthetic@example.test")
        app.login_password_var.set("synthetic-login-password")
        app.name_var.set("Synthetic Tester")
        app.email_var.set("synthetic@example.test")
        app.phone_var.set("555-0100")
        app.address1_var.set("1 Synthetic Test Lane")
        app.city_var.set("Test City")
        app.region_var.set("TS")
        app.postal_var.set("00000")
        app.country_var.set("US")
        app.card_number_var.set("4111111111111111")
        app.card_expiry_var.set("12/2030")
        app.card_cvv_var.set("123")
        app.card_holder_var.set("Synthetic Tester")
        app.order_limit_var.set("70.00")
        app.schedule_mode_var.set("immediate")
        app.window_var.set("60")
        assert app._save_settings(silent=True)
        root.update()
        assert paths.config.is_file()
        assert app._card_number_masked.endswith("1111")
        assert "4111111111111111" not in app.card_number_var.get()
        assert app.login_password_var.get() == "********"
        assert "准备就绪后开始" in app.time_var.get()
        assert "提交截止" not in app.time_var.get()
        assert "剩余" not in app.time_var.get()
        app._use_editor_target()
        assert app._direct_target is not None
        assert app._selected_preset_id is None
        assert SHIPPING_POLICY_TEXT  # visible policy is a fixed string
    finally:
        root.destroy()

    loaded = load_config(paths.config)
    assert loaded.ok
    assert loaded.config is not None
    assert loaded.config.login.email == "login-synthetic@example.test"
    assert loaded.config.login.password == "synthetic-login-password"
    root = new_root()
    try:
        app2 = ToppsAutoBotApp(root, paths=paths)
        root.update()
        assert app2.config is not None
        assert app2.card_number_var.get().endswith("1111")
        assert app2.login_email_var.get() == "login-synthetic@example.test"
        assert app2.login_password_var.get() == "********"
        assert "尚未运行就绪" in app2.readiness_var.get()
        assert app2._prepared_level is PreparedLevel.NONE
        assert not app2.auto_login_var.get()
        app2.login_email_var.set("")
        app2.show_login_var.set(True)
        app2._toggle_login()
        app2.login_password_var.set("")
        assert app2._save_settings(silent=True)
        saved = load_config(paths.config).config
        assert saved.login.email == saved.login.password == ""
        assert saved.card == loaded.config.card
        assert saved.presets == loaded.config.presets
    finally:
        root.destroy()


def test_ui_opens_browser_before_purchase_settings(tmp_path, monkeypatch):
    from topps_auto_bot import ui
    from topps_auto_bot.driver_setup import ChromeInstallation, ValidationResult
    from topps_auto_bot.models import DriverSettings

    root = new_root()
    paths = AppPaths.under(tmp_path / "data")
    try:
        app = ToppsAutoBotApp(root, paths)
        assert app.config is None
        assert not app.auto_login_var.get()
        app.driver_settings = DriverSettings(path="controlled-driver", sha256="checked")
        chrome = ChromeInstallation(tmp_path / "controlled-chrome", "156.0.8078.80", "mac-arm64")
        monkeypatch.setattr(ui, "detect_chrome", lambda: chrome)
        monkeypatch.setattr(ui, "inspect_chrome", lambda path: chrome)
        monkeypatch.setattr(ui, "check_enabled_driver", lambda *_args, **_kwargs: ValidationResult(True, chrome.version, chrome.architecture))

        class Session:
            def navigate(self, _url):
                pass

            def public_access_error(self):
                return "官网要求人工处理挑战"

            def launch_arguments(self):
                return ("--disable-blink-features=AutomationControlled",)

        monkeypatch.setattr(ui, "open_live_session", lambda **_kwargs: Session())
        assert app._open_browser()
        assert "UC 启动通过" in app.driver_status_var.get()
        assert "官网要求人工处理挑战" in app.status_var.get()
        assert not paths.config.exists()
        messages = []
        monkeypatch.setattr(ui.messagebox, "showerror", lambda *args: messages.append(args))
        app.no_sandbox_var.set(True)
        assert not app._open_browser()
        assert messages[0][0] == "驱动需验证"
        assert app.session is not None
    finally:
        root.destroy()


def test_ui_blocks_save_while_task_is_active(tmp_path: Path, monkeypatch) -> None:
    paths = AppPaths.under(tmp_path / "data")
    root = new_root()
    try:
        app = ToppsAutoBotApp(root, paths=paths)
        monkeypatch.setattr(app.manager, "is_active", lambda: True)
        monkeypatch.setattr(messagebox, "showerror", lambda *args, **kwargs: None)
        assert not app._save_settings(silent=True)
    finally:
        root.destroy()


def test_ui_pending_panel_reflects_blocking_record(tmp_path: Path) -> None:
    paths = AppPaths.under(tmp_path / "data")
    paths.ensure_directories()
    config = make_config()
    preset = config.selected_preset()
    assert preset is not None
    now = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
    store = SubmissionStore(paths.run_state)
    store.begin(
        build_submission_record(
            task_id="task-ui",
            preset=preset,
            order_limit_usd=Decimal("70.00"),
            prepared_level=PreparedLevel.CHECKOUT_READY,
            planned_start_utc=None,
            deadline_utc=now,
            shipping=shipping_snapshot(),
            total_usd=Decimal("55.00"),
            now=now,
        )
    )
    root = new_root()
    try:
        app = ToppsAutoBotApp(root, paths=paths)
        root.update()
        assert "未决付款" in app.pending_var.get()
        app.store.resolve_purchase(
            state=SubmissionState.NOT_SUBMITTED,
            note="用户核对后确认未形成订单",
            now=now,
        )
        app._refresh_pending()
        assert "最近购买记录" in app.pending_var.get()
    finally:
        root.destroy()


def test_ui_driver_cancel_then_manual_updates_only_after_completion(tmp_path: Path, monkeypatch) -> None:
    import threading
    import time

    from topps_auto_bot import ui as ui_module
    from topps_auto_bot.driver_setup import (
        ActivatedDriver,
        ChromeInstallation,
        DriverOperationCancelled,
        DownloadProgress,
    )
    from topps_auto_bot.models import DriverSettings
    from topps_auto_bot.settings import save_config

    paths = AppPaths.under(tmp_path / "data")
    paths.ensure_directories()
    save_config(paths.config, make_config())
    root = new_root()
    started = threading.Event()

    def fake_automatic(**kwargs):
        started.set()
        kwargs["progress"](DownloadProgress(10, None, "downloading"))
        while not kwargs["operation"].cancelled:
            time.sleep(0.01)
        raise DriverOperationCancelled("cancelled")

    def wait_for(predicate) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            root.update()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("等待 UI 状态超时")

    try:
        app = ToppsAutoBotApp(root, paths=paths)
        app.driver_chrome = ChromeInstallation(Path("/fake/chrome"), "156.0.8078.80", "mac-arm64")
        monkeypatch.setattr(ui_module, "prepare_automatic_driver", fake_automatic)
        app._download_driver()
        assert started.wait(timeout=2)
        app._cancel_driver()
        wait_for(lambda: app.driver_operation is None)
        assert "取消" in app.progress_var.get() or "cancelled" in app.progress_var.get()

        selected = tmp_path / "chromedriver"
        selected.write_bytes(b"fake")
        monkeypatch.setattr(ui_module.filedialog, "askopenfilename", lambda **kwargs: str(selected))

        def fake_manual(**kwargs):
            settings = DriverSettings(
                mode="manual",
                path=str(selected),
                version="156.0.8078.80",
                platform="mac-arm64",
            )
            kwargs["store_selection"](settings)
            return ActivatedDriver(settings=settings, path=selected, version="156.0.8078.80")

        monkeypatch.setattr(ui_module, "prepare_manual_driver", fake_manual)
        app._manual_driver()
        wait_for(lambda: app.driver_settings.mode == "manual")
        assert "驱动已启用" in app.progress_var.get()
        assert app.config is not None
        assert app.driver_settings.mode == "manual"
    finally:
        root.destroy()


@pytest.mark.parametrize("has_saved_preset", [False, True])
def test_ui_temporary_target_can_save_purchase_details(tmp_path, monkeypatch, has_saved_preset):
    from topps_auto_bot.settings import save_config

    root = new_root()
    paths = AppPaths.under(tmp_path / "data")
    paths.ensure_directories()
    if has_saved_preset:
        save_config(paths.config, make_config())
    monkeypatch.setattr(messagebox, "showerror", lambda title, message: pytest.fail(f"{title}: {message}"))
    try:
        app = ToppsAutoBotApp(root, paths)
        fields = {
            "name": "Synthetic Tester", "email": "synthetic@example.test", "phone": "555-0100",
            "address1": "1 Synthetic Test Lane", "city": "Test City", "region": "TS",
            "postal": "00000", "country": "US", "card_number": "4111111111111111",
            "card_expiry": "12/2030", "card_cvv": "123", "card_holder": "Synthetic Tester",
            "order_limit": "70.00",
            "preset_title": "Synthetic direct", "preset_variant": "Retail Box",
            "preset_url": "https://www.topps.com/products/synthetic-direct",
            "preset_quantity": "2", "preset_price": "25.00",
        }
        for name, value in fields.items():
            getattr(app, name + "_var").set(value)

        app._use_editor_target()
        target = app._direct_target
        assert target is not None and target.variant == "Retail Box"
        app.order_limit_var.set("82.00")
        app.card_expiry_var.set("11/2031")
        assert app._save_settings(silent=True)
        loaded = load_config(paths.config).config
        assert loaded is not None
        assert loaded.order_limit_usd == Decimal("82.00")
        assert (loaded.card.expiry_month, loaded.card.expiry_year) == (11, 2031)
        assert loaded.presets == (make_config().presets if has_saved_preset else ())
        assert loaded.target_snapshot() is None
        assert app._direct_target == target
    finally:
        root.destroy()
