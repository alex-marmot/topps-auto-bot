from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support import make_config, make_login, make_preset
from topps_auto_bot.models import DomainError, LoginCredentials, ScheduleMode, ScheduleSettings, UTC, parse_beijing_datetime
from topps_auto_bot.settings import (
    AppPaths,
    ConfigErrorKind,
    ConfigSaveError,
    config_from_dict,
    config_to_dict,
    default_data_root,
    load_config,
    masked_config_dict,
    parse_card_expiry,
    replace_invalid_config,
    save_config,
    validate_config,
    validate_product_url,
    validate_window_seconds,
)


def test_default_data_root_per_platform(tmp_path: Path) -> None:
    assert default_data_root(platform_name="win32", environ={"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}, home=tmp_path) == Path(
        "C:/Users/Test/AppData/Local/ToppsAutoBot"
    )
    assert default_data_root(platform_name="darwin", environ={}, home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "ToppsAutoBot"
    )
    assert default_data_root(platform_name="linux", environ={"XDG_DATA_HOME": "/data"}, home=tmp_path) == Path(
        "/data/ToppsAutoBot"
    )


def test_valid_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = make_config()
    save_config(path, config)
    loaded = load_config(path)
    assert loaded.ok
    assert loaded.config is not None
    assert loaded.config.presets == config.presets
    assert loaded.config.target_snapshot() is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["card"]["number"] == "4111111111111111"
    assert loaded.config.login == config.login


def test_missing_config_distinct_from_corrupt(tmp_path: Path) -> None:
    missing = load_config(tmp_path / "config.json")
    assert missing.status is ConfigErrorKind.MISSING
    assert missing.config is None


def test_unselected_or_missing_target_rejected(tmp_path: Path) -> None:
    with pytest.raises(DomainError):
        validate_config(make_config(selected=None))
    config = make_config()
    payload = config_to_dict(config)
    payload["selected_preset_id"] = "missing"
    with pytest.raises(DomainError):
        config_from_dict(payload)


@pytest.mark.parametrize("value", [0, -1, 1.5, True, 601, "", "abc", 600.0])
def test_invalid_window_rejected(value: object) -> None:
    with pytest.raises(DomainError):
        validate_window_seconds(value)


@pytest.mark.parametrize("value", [1, 60, 600, "120"])
def test_valid_window_accepted(value: object) -> None:
    assert validate_window_seconds(value) in (1, 60, 600, 120)


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, "", "2.0", "abc"])
def test_invalid_quantity_rejected(quantity: object) -> None:
    config = make_config(presets=(make_preset(quantity=1),))
    payload = config_to_dict(config)
    payload["presets"][0]["quantity"] = quantity
    with pytest.raises(DomainError):
        config_from_dict(payload)


@pytest.mark.parametrize("amount", ["0", "-1.00", "1.999", "NaN", "Infinity", "", True, 1.5])
def test_invalid_money_rejected(amount: object) -> None:
    config = make_config()
    payload = config_to_dict(config)
    payload["presets"][0]["unit_price_limit_usd"] = amount
    with pytest.raises(DomainError):
        config_from_dict(payload)


def test_duplicate_presets_rejected() -> None:
    first = make_preset(preset_id="a")
    second = make_preset(preset_id="b", quantity=1)
    with pytest.raises(DomainError, match="重复"):
        validate_config(make_config(presets=(first, second), selected="a"))


def test_duplicate_url_and_variant_rejected() -> None:
    first = make_preset(preset_id="a", title="Different title")
    second = make_preset(preset_id="b", title="Another title")
    with pytest.raises(DomainError, match="重复"):
        validate_config(make_config(presets=(first, second), selected="a"))


@pytest.mark.parametrize(
    "url",
    [
        "http://www.topps.com/products/x",
        "https://topps.com.example.invalid/products/x",
        "https://user:pass@www.topps.com/products/x",
        "https://www.topps.com:8443/products/x",
        "https://www.topps.com/",
    ],
)
def test_untrusted_product_urls_rejected(url: str) -> None:
    with pytest.raises(DomainError):
        validate_product_url(url)


def test_corrupt_and_future_config_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.status is ConfigErrorKind.CORRUPT
    assert path.read_text(encoding="utf-8") == "{broken"
    with pytest.raises(ConfigSaveError):
        save_config(path, make_config())
    assert path.read_text(encoding="utf-8") == "{broken"

    future = config_to_dict(make_config())
    future["schema_version"] = 99
    path.write_text(json.dumps(future), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.status is ConfigErrorKind.UNSUPPORTED_VERSION
    with pytest.raises(ConfigSaveError):
        save_config(path, make_config())
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 99


def test_controlled_migration_only_with_explicit_rule(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    legacy = config_to_dict(make_config())
    legacy["schema_version"] = 0
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.status is ConfigErrorKind.UNSUPPORTED_VERSION

    def migrate_v0(data: dict) -> dict:
        data["schema_version"] = 1
        return data

    def migrate_v1(data: dict) -> dict:
        data["schema_version"] = 2
        data["login"] = {"email": "", "password": ""}
        return data

    migrated = load_config(path, migrations={0: migrate_v0, 1: migrate_v1})
    assert migrated.ok
    assert migrated.config is not None


def test_v1_config_migrates_with_login_prompt_and_preserves_trade_data(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    legacy = config_to_dict(make_config())
    legacy["schema_version"] = 1
    legacy.pop("login", None)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.ok
    assert loaded.config is not None
    assert loaded.config.schema_version == 2
    assert loaded.config.login.email == ""
    assert loaded.config.card == make_config().card
    with pytest.raises(DomainError):
        save_config(path, loaded.config)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_migration_failure_preserves_original(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    legacy = config_to_dict(make_config())
    legacy["schema_version"] = 0
    original = json.dumps(legacy)
    path.write_text(original, encoding="utf-8")

    def fail(_data: dict) -> dict:
        raise RuntimeError("synthetic migration failure")

    loaded = load_config(path, migrations={0: fail})
    assert loaded.status is ConfigErrorKind.MIGRATION_FAILED
    assert path.read_text(encoding="utf-8") == original


def test_save_failure_keeps_old_config_and_cleans_temp(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(path, make_config())
    original = path.read_text(encoding="utf-8")
    new_config = make_config(order_limit="99.00")

    def fail_before_replace(_temp: Path) -> None:
        raise OSError("synthetic disk failure")

    with pytest.raises(ConfigSaveError):
        save_config(path, new_config, before_replace=fail_before_replace)
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_explicit_repair_backs_up_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    replace_invalid_config(path, make_config())
    assert load_config(path).ok
    backups = list(tmp_path.glob("config.json.invalid-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"


def test_masked_config_hides_card_and_cvv() -> None:
    masked = masked_config_dict(make_config())
    assert masked["card"]["number"].endswith("1111")
    assert "4111111111111111" not in json.dumps(masked)
    assert masked["card"]["cvv"] == "***"
    assert masked["login"]["password"] == "********"
    assert "synthetic-login-password" not in json.dumps(masked)


def test_scheduled_config_roundtrip() -> None:
    start = parse_beijing_datetime("2026-11-07 09:30:00")
    config = make_config(
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=start, window_seconds=120)
    )
    payload = config_to_dict(config)
    assert payload["schedule"]["scheduled_start_utc"].endswith("+00:00")
    parsed = config_from_dict(payload)
    assert parsed.schedule.scheduled_start_utc == start


def test_parse_card_expiry() -> None:
    assert parse_card_expiry("12/2030") == (12, 2030)
    assert parse_card_expiry("1/2030") == (1, 2030)
    with pytest.raises(DomainError):
        parse_card_expiry("13/2030")


def test_app_paths_under(tmp_path: Path) -> None:
    paths = AppPaths.under(tmp_path)
    assert paths.config == tmp_path / "config.json"
    assert paths.run_state == tmp_path / "run-state.json"
    paths.ensure_directories()
    assert paths.browser_profile.is_dir()
    assert paths.drivers.is_dir()
    assert paths.driver_staging.is_dir()
    if os.name != "nt":
        assert stat_mode(paths.root) == 0o700


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_login_email_is_independent_from_shipping_email() -> None:
    login = make_login(email="account-owner@example.test", password="")
    config = make_config(login=login)
    payload = config_to_dict(config)
    assert payload["login"]["email"] == "account-owner@example.test"
    assert payload["customer"]["email"] != payload["login"]["email"]
    parsed = config_from_dict(payload)
    assert parsed.login == login
    assert parsed.customer.email == config.customer.email


def test_login_email_requires_valid_address_when_saving(tmp_path: Path) -> None:
    with pytest.raises(DomainError):
        validate_config(make_config(login=make_login(email="not-an-email")), require_login=True)
    with pytest.raises(DomainError):
        save_config(tmp_path / "config.json", make_config(login=LoginCredentials(email="")))


def test_old_invalid_window_does_not_block_new_task(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    payload = config_to_dict(make_config())
    payload["schedule"]["window_seconds"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.ok
    assert loaded.config is not None
    assert loaded.config.schedule.window_seconds == 60
