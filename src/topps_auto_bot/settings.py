from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .models import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    MIN_WINDOW_SECONDS,
    AppConfig,
    Card,
    Customer,
    DomainError,
    DriverSettings,
    LoginCredentials,
    ProductPreset,
    ScheduleMode,
    ScheduleSettings,
    USD,
    parse_beijing_datetime,
    parse_quantity,
    parse_usd,
    parse_utc_iso,
    utc_now_iso,
)
from .redaction import mask_card_number, mask_cvv, mask_password

CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
ALLOWED_TOPPs_HOSTS = frozenset({"topps.com", "www.topps.com"})
ALLOWED_TOPPs_DISPLAY_HOSTS = frozenset({"topps.com", "www.topps.com"})

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CARD_NUMBER = re.compile(r"^\d{12,19}$")
_CVV = re.compile(r"^\d{3,4}$")
_EXPIRY_MONTH = re.compile(r"^(?:0?[1-9]|1[0-2])$")


class ConfigErrorKind(StrEnum):
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    INVALID = "INVALID"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    MIGRATION_FAILED = "MIGRATION_FAILED"
    IO_ERROR = "IO_ERROR"


class ConfigSaveError(RuntimeError):
    pass


class ConfigError(RuntimeError):
    def __init__(self, kind: ConfigErrorKind, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.path = path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    config: Path
    run_state: Path
    browser_profile: Path
    drivers: Path
    driver_staging: Path
    logs: Path

    @classmethod
    def under(cls, root: Path) -> "AppPaths":
        root = Path(root)
        return cls(
            root=root,
            config=root / "config.json",
            run_state=root / "run-state.json",
            browser_profile=root / "browser-profile",
            drivers=root / "drivers",
            driver_staging=root / "driver-staging",
            logs=root / "logs",
        )

    def ensure_directories(self) -> None:
        for path in (
            self.root,
            self.browser_profile,
            self.drivers,
            self.driver_staging,
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass


def default_data_root(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform_name = platform_name or sys.platform
    environ = environ if environ is not None else os.environ
    home = Path(home) if home is not None else Path.home()
    if platform_name.startswith("win"):
        base = environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "ToppsAutoBot"
        return home / "AppData" / "Local" / "ToppsAutoBot"
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "ToppsAutoBot"
    xdg = environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "ToppsAutoBot"
    return home / ".local" / "share" / "ToppsAutoBot"


def default_paths() -> AppPaths:
    return AppPaths.under(default_data_root())


def validate_product_url(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise DomainError("商品链接不能为空")
    if any(ch.isspace() for ch in url):
        raise DomainError("商品链接不能包含空白字符")
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise DomainError("商品链接必须使用 HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise DomainError("商品链接不能包含用户信息")
    host = (parsed.hostname or "").casefold()
    if host not in ALLOWED_TOPPs_HOSTS:
        raise DomainError("商品链接必须精确指向 topps.com 或 www.topps.com")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DomainError("商品链接端口无效") from exc
    if port not in (None, 443):
        raise DomainError("商品链接只能使用默认 HTTPS 端口")
    if not parsed.path or parsed.path == "/":
        raise DomainError("商品链接必须指向具体商品页面")


def validate_window_seconds(value: object) -> int:
    if isinstance(value, bool):
        raise DomainError("抢购窗口必须是 1–600 的整数秒")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip())
    else:
        raise DomainError("抢购窗口必须是 1–600 的整数秒")
    if not MIN_WINDOW_SECONDS <= seconds <= MAX_WINDOW_SECONDS:
        raise DomainError("抢购窗口必须是 1–600 的整数秒")
    return seconds


def validate_schedule(schedule: ScheduleSettings, *, now: datetime | None = None) -> None:
    if schedule.mode not in (ScheduleMode.IMMEDIATE, ScheduleMode.SCHEDULED):
        raise DomainError("抢购模式必须是立即或定时")
    if schedule.mode is ScheduleMode.SCHEDULED:
        if schedule.scheduled_start_utc is None:
            raise DomainError("定时模式必须设置开售时间")
        if now is not None and schedule.scheduled_start_utc <= now.astimezone(schedule.scheduled_start_utc.tzinfo):
            raise DomainError("定时开售时间必须晚于当前时间")


def _validate_preset_selection(config: AppConfig) -> None:
    preset_ids = [preset.preset_id for preset in config.presets]
    if any(not preset_id.strip() for preset_id in preset_ids):
        raise DomainError("预设标识不能为空")
    if len(set(preset_ids)) != len(preset_ids):
        raise DomainError("预设标识不能重复")
    if config.selected_preset_id is not None and config.selected_preset_id not in preset_ids:
        raise DomainError("所选预设不存在")


def validate_preset(preset: ProductPreset) -> None:
    if not preset.preset_id.strip():
        raise DomainError("预设标识不能为空")
    if not preset.title.strip():
        raise DomainError("商品名称不能为空")
    if not preset.variant.strip():
        raise DomainError("商品规格不能为空")
    validate_product_url(preset.url)
    parse_quantity(preset.quantity)
    parse_usd(preset.unit_price_limit_usd)


def detect_duplicate_presets(presets: tuple[ProductPreset, ...]) -> list[tuple[ProductPreset, ProductPreset]]:
    duplicates: list[tuple[ProductPreset, ProductPreset]] = []
    for index, left in enumerate(presets):
        for right in presets[index + 1 :]:
            same_identity = left.identity_key == right.identity_key
            same_url_variant = left.url_key == right.url_key and left.identity_key[1] == right.identity_key[1]
            if same_identity or same_url_variant:
                duplicates.append((left, right))
    return duplicates


def validate_config(
    config: AppConfig,
    *,
    now: datetime | None = None,
    require_login: bool = False,
) -> None:
    if config.schema_version != CURRENT_SCHEMA_VERSION:
        raise DomainError("配置格式版本不受支持")
    _validate_login(config.login, required=require_login)
    for preset in config.presets:
        validate_preset(preset)
    _validate_preset_selection(config)
    duplicates = detect_duplicate_presets(config.presets)
    if duplicates:
        left, right = duplicates[0]
        raise DomainError(f"预设重复：{left.title} / {left.variant} 与 {right.title} / {right.variant}")
    parse_usd(config.order_limit_usd)
    _validate_customer(config.customer)
    _validate_card(config.card)
    validate_schedule(config.schedule, now=now)
    if config.driver.mode not in ("automatic", "manual"):
        raise DomainError("驱动模式必须是 automatic 或 manual")
    if not isinstance(config.driver.no_sandbox, bool):
        raise DomainError("no_sandbox 必须是布尔值")


def _validate_login(login: LoginCredentials, *, required: bool) -> None:
    if not isinstance(login, LoginCredentials):
        raise DomainError("登录资料无效")
    email = login.email.strip()
    if not email:
        if required:
            raise DomainError("登录账号不能为空")
        return
    if not _EMAIL.fullmatch(email):
        raise DomainError("登录账号必须是有效邮箱")
    if not isinstance(login.password, str):
        raise DomainError("登录密码必须是文本")


def _validate_customer(customer: Customer) -> None:
    required = {
        "收货人姓名": customer.full_name,
        "电子邮箱": customer.email,
        "联系电话": customer.phone,
        "地址第一行": customer.address_line1,
        "城市": customer.city,
        "州/省": customer.region,
        "邮政编码": customer.postal_code,
        "国家": customer.country,
    }
    missing = [label for label, value in required.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise DomainError("收货信息缺少：" + "、".join(missing))
    if not _EMAIL.fullmatch(customer.email.strip()):
        raise DomainError("电子邮箱格式无效")


def _validate_card(card: Card) -> None:
    if not isinstance(card, Card):
        raise DomainError("信用卡资料无效")
    if not _CARD_NUMBER.fullmatch(card.number):
        raise DomainError("卡号必须是 12–19 位数字")
    if not 1 <= card.expiry_month <= 12:
        raise DomainError("信用卡有效期月份必须是 1–12")
    if not 2000 <= card.expiry_year <= 2100:
        raise DomainError("信用卡有效期年份无效")
    if not _CVV.fullmatch(card.cvv):
        raise DomainError("CVV 必须是 3–4 位数字")
    if not card.holder_name.strip():
        raise DomainError("持卡人姓名不能为空")


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    schedule: dict[str, Any] = {
        "mode": str(config.schedule.mode),
        "scheduled_start_utc": (
            config.schedule.scheduled_start_utc.astimezone(timezone.utc).isoformat()
            if config.schedule.scheduled_start_utc is not None
            else None
        ),
        "window_seconds": config.schedule.window_seconds,
    }
    return {
        "schema_version": config.schema_version,
        "presets": [
            {
                "id": preset.preset_id,
                "title": preset.title,
                "variant": preset.variant,
                "url": preset.url,
                "quantity": preset.quantity,
                "unit_price_limit_usd": f"{preset.unit_price_limit_usd:.2f}",
            }
            for preset in config.presets
        ],
        "selected_preset_id": config.selected_preset_id,
        "order_limit_usd": f"{config.order_limit_usd:.2f}",
        "login": {
            "email": config.login.email,
            "password": config.login.password,
        },
        "customer": {
            "full_name": config.customer.full_name,
            "email": config.customer.email,
            "phone": config.customer.phone,
            "address_line1": config.customer.address_line1,
            "address_line2": config.customer.address_line2,
            "city": config.customer.city,
            "region": config.customer.region,
            "postal_code": config.customer.postal_code,
            "country": config.customer.country,
        },
        "card": {
            "number": config.card.number,
            "expiry_month": config.card.expiry_month,
            "expiry_year": config.card.expiry_year,
            "cvv": config.card.cvv,
            "holder_name": config.card.holder_name,
        },
        "driver": {
            "mode": config.driver.mode,
            "path": config.driver.path,
            "version": config.driver.version,
            "platform": config.driver.platform,
            "no_sandbox": config.driver.no_sandbox,
            "sha256": config.driver.sha256,
        },
        "schedule": schedule,
    }


def masked_config_dict(config: AppConfig) -> dict[str, Any]:
    payload = config_to_dict(config)
    payload["login"]["password"] = mask_password(config.login.password)
    payload["card"]["number"] = mask_card_number(config.card.number)
    payload["card"]["cvv"] = mask_cvv(config.card.cvv)
    return payload


def _require(mapping: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        raise DomainError(f"配置缺少字段：{label}")
    return mapping[key]


def _require_text(mapping: Mapping[str, Any], key: str, label: str, *, allow_empty: bool = False) -> str:
    value = _require(mapping, key, label)
    if not isinstance(value, str):
        raise DomainError(f"{label}必须是文本")
    if not allow_empty and not value.strip():
        raise DomainError(f"{label}不能为空")
    return value


def _parse_int_field(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise DomainError(f"{label}必须是整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise DomainError(f"{label}必须是整数")
    if not minimum <= parsed <= maximum:
        raise DomainError(f"{label}必须在 {minimum}–{maximum} 之间")
    return parsed


def config_from_dict(payload: Mapping[str, Any]) -> AppConfig:
    if not isinstance(payload, Mapping):
        raise DomainError("配置根节点必须是对象")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise DomainError("配置缺少有效的 schema_version")
    presets_payload = _require(payload, "presets", "presets")
    if not isinstance(presets_payload, list):
        raise DomainError("presets 必须是数组")
    presets: list[ProductPreset] = []
    for index, item in enumerate(presets_payload):
        if not isinstance(item, Mapping):
            raise DomainError(f"第 {index + 1} 个预设必须是对象")
        presets.append(
            ProductPreset(
                preset_id=_require_text(item, "id", f"第 {index + 1} 个预设标识"),
                title=_require_text(item, "title", f"第 {index + 1} 个商品名称"),
                variant=_require_text(item, "variant", f"第 {index + 1} 个商品规格"),
                url=_require_text(item, "url", f"第 {index + 1} 个商品链接"),
                quantity=parse_quantity(_require(item, "quantity", f"第 {index + 1} 个数量")),
                unit_price_limit_usd=parse_usd(
                    _require(item, "unit_price_limit_usd", f"第 {index + 1} 个单价上限")
                ),
            )
        )
    selected = payload.get("selected_preset_id")
    if selected is not None and not isinstance(selected, str):
        raise DomainError("selected_preset_id 必须是文本或 null")
    login_payload = payload.get("login", {})
    if not isinstance(login_payload, Mapping):
        raise DomainError("login 必须是对象")
    login_email = login_payload.get("email", "")
    login_password = login_payload.get("password", "")
    if not isinstance(login_email, str) or not isinstance(login_password, str):
        raise DomainError("登录账号和密码必须是文本")
    customer_payload = _require(payload, "customer", "customer")
    card_payload = _require(payload, "card", "card")
    driver_payload = _require(payload, "driver", "driver")
    schedule_payload = _require(payload, "schedule", "schedule")
    if not all(
        isinstance(item, Mapping)
        for item in (customer_payload, card_payload, driver_payload, schedule_payload)
    ):
        raise DomainError("customer、card、driver、schedule 必须是对象")

    customer = Customer(
        full_name=_require_text(customer_payload, "full_name", "收货人姓名"),
        email=_require_text(customer_payload, "email", "电子邮箱"),
        phone=_require_text(customer_payload, "phone", "联系电话"),
        address_line1=_require_text(customer_payload, "address_line1", "地址第一行"),
        address_line2=_require_text(customer_payload, "address_line2", "地址第二行", allow_empty=True),
        city=_require_text(customer_payload, "city", "城市"),
        region=_require_text(customer_payload, "region", "州/省"),
        postal_code=_require_text(customer_payload, "postal_code", "邮政编码"),
        country=_require_text(customer_payload, "country", "国家"),
    )
    card = Card(
        number=_require_text(card_payload, "number", "卡号"),
        expiry_month=_parse_int_field(_require(card_payload, "expiry_month", "有效期月份"), "有效期月份", minimum=1, maximum=12),
        expiry_year=_parse_int_field(_require(card_payload, "expiry_year", "有效期年份"), "有效期年份", minimum=2000, maximum=2100),
        cvv=_require_text(card_payload, "cvv", "CVV"),
        holder_name=_require_text(card_payload, "holder_name", "持卡人姓名"),
    )
    driver = DriverSettings(
        mode=_require_text(driver_payload, "mode", "驱动模式"),
        path=driver_payload.get("path") if driver_payload.get("path") is None or isinstance(driver_payload.get("path"), str) else None,
        version=driver_payload.get("version") if driver_payload.get("version") is None or isinstance(driver_payload.get("version"), str) else None,
        platform=driver_payload.get("platform") if driver_payload.get("platform") is None or isinstance(driver_payload.get("platform"), str) else None,
        no_sandbox=bool(driver_payload.get("no_sandbox", False)),
        sha256=driver_payload.get("sha256"),
    )
    mode_text = _require_text(schedule_payload, "mode", "抢购模式")
    try:
        schedule_mode = ScheduleMode(mode_text)
    except ValueError as exc:
        raise DomainError("抢购模式必须是 immediate 或 scheduled") from exc
    scheduled_start_raw = schedule_payload.get("scheduled_start_utc")
    if scheduled_start_raw is not None and not isinstance(scheduled_start_raw, str):
        raise DomainError("scheduled_start_utc 必须是文本或 null")
    scheduled_start = parse_utc_iso(scheduled_start_raw) if scheduled_start_raw else None
    try:
        legacy_window = validate_window_seconds(
            schedule_payload.get("window_seconds", DEFAULT_WINDOW_SECONDS)
        )
    except DomainError:
        legacy_window = DEFAULT_WINDOW_SECONDS
    schedule = ScheduleSettings(
        mode=schedule_mode,
        scheduled_start_utc=scheduled_start,
        window_seconds=legacy_window,
    )
    config = AppConfig(
        schema_version=version,
        presets=tuple(presets),
        selected_preset_id=selected,
        order_limit_usd=parse_usd(_require(payload, "order_limit_usd", "整单上限")),
        login=LoginCredentials(email=login_email, password=login_password),
        customer=customer,
        card=card,
        driver=driver,
        schedule=schedule,
    )
    validate_config(config)
    return config


Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("login", {"email": "", "password": ""})
    payload["schema_version"] = 2
    return payload


MIGRATIONS: dict[int, Migration] = {1: _migrate_v1_to_v2}


@dataclass(frozen=True)
class LoadedConfig:
    status: ConfigErrorKind | None
    config: AppConfig | None
    error: str | None = None
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status is None and self.config is not None


def _migrate(
    payload: dict[str, Any],
    *,
    target_version: int,
    migrations: Mapping[int, Migration],
) -> dict[str, Any]:
    current = payload.get("schema_version")
    if isinstance(current, bool) or not isinstance(current, int):
        raise ConfigError(ConfigErrorKind.CORRUPT, "配置缺少有效的 schema_version")
    if current > target_version:
        raise ConfigError(
            ConfigErrorKind.UNSUPPORTED_VERSION,
            f"配置版本 {current} 高于当前支持版本 {target_version}",
        )
    migrated = payload
    version = current
    while version < target_version:
        migration = migrations.get(version)
        if migration is None:
            raise ConfigError(
                ConfigErrorKind.UNSUPPORTED_VERSION,
                f"没有从配置版本 {version} 到 {version + 1} 的受控迁移规则",
            )
        try:
            migrated = migration(dict(migrated))
        except Exception as exc:  # noqa: BLE001 - migration failure must preserve data
            raise ConfigError(ConfigErrorKind.MIGRATION_FAILED, f"配置迁移失败：{exc}") from exc
        if not isinstance(migrated, dict):
            raise ConfigError(ConfigErrorKind.MIGRATION_FAILED, "配置迁移没有返回有效对象")
        next_version = migrated.get("schema_version")
        if isinstance(next_version, bool) or not isinstance(next_version, int) or next_version != version + 1:
            raise ConfigError(ConfigErrorKind.MIGRATION_FAILED, "配置迁移没有正确推进版本号")
        version = next_version
    return migrated


def load_config(path: Path, *, migrations: Mapping[int, Migration] | None = None) -> LoadedConfig:
    path = Path(path)
    if not path.exists():
        return LoadedConfig(status=ConfigErrorKind.MISSING, config=None, path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedConfig(status=ConfigErrorKind.IO_ERROR, config=None, error=str(exc), path=path)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return LoadedConfig(
            status=ConfigErrorKind.CORRUPT,
            config=None,
            error=f"配置文件不是有效 JSON：{exc.msg}",
            path=path,
        )
    if not isinstance(payload, dict):
        return LoadedConfig(status=ConfigErrorKind.CORRUPT, config=None, error="配置根节点必须是对象", path=path)
    try:
        migrated = _migrate(
            payload,
            target_version=CURRENT_SCHEMA_VERSION,
            migrations=MIGRATIONS if migrations is None else migrations,
        )
        config = config_from_dict(migrated)
    except ConfigError as exc:
        return LoadedConfig(status=exc.kind, config=None, error=exc.message, path=path)
    except (DomainError, ValueError, TypeError) as exc:
        return LoadedConfig(status=ConfigErrorKind.INVALID, config=None, error=str(exc), path=path)
    return LoadedConfig(status=None, config=config, path=path)


def _secure_temp_path(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    os.close(descriptor)
    return Path(raw_path)


def load_driver_selection(path: Path, *, fallback: DriverSettings = DriverSettings()) -> DriverSettings:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DriverSettings(**payload)
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError, TypeError):
        return DriverSettings()


def save_driver_selection(path: Path, settings: DriverSettings) -> None:
    temp_path = _secure_temp_path(path)
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(settings), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def save_config(
    path: Path,
    config: AppConfig,
    *,
    now: datetime | None = None,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    path = Path(path)
    validate_config(config, now=now)
    existing = load_config(path)
    if existing.status in (
        ConfigErrorKind.CORRUPT,
        ConfigErrorKind.UNSUPPORTED_VERSION,
        ConfigErrorKind.MIGRATION_FAILED,
        ConfigErrorKind.IO_ERROR,
    ):
        raise ConfigSaveError("已有配置无法可靠读取，必须显式修复或备份后再保存")
    temp_path = _secure_temp_path(path)
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config_to_dict(config), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        if before_replace is not None:
            before_replace(temp_path)
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, ConfigSaveError):
            raise
        raise ConfigSaveError(str(exc)) from exc


def replace_invalid_config(path: Path, config: AppConfig) -> None:
    """Explicit recovery path: back up an unreadable file, then write a new one."""
    path = Path(path)
    if path.exists():
        backup = path.with_name(f"{path.name}.invalid-{secrets.token_hex(4)}.bak")
        path.replace(backup)
        try:
            backup.chmod(0o600)
        except OSError:
            pass
    save_config(path, config)


def build_schedule_from_form(
    *,
    mode: ScheduleMode,
    beijing_text: str | None,
    window_seconds: object,
    now: datetime | None = None,
) -> ScheduleSettings:
    seconds = validate_window_seconds(window_seconds)
    scheduled = parse_beijing_datetime(beijing_text) if beijing_text else None
    result = ScheduleSettings(mode=mode, scheduled_start_utc=scheduled, window_seconds=seconds)
    validate_schedule(result, now=now)
    return result


def config_with_driver(config: AppConfig, driver: DriverSettings) -> AppConfig:
    return replace(config, driver=driver)


def config_with_schedule(config: AppConfig, schedule: ScheduleSettings) -> AppConfig:
    return replace(config, schedule=schedule)


def utc_stamp(now: datetime) -> str:
    return utc_now_iso(now)


def validate_money_text(value: str, *, allow_zero: bool = False) -> Decimal:
    return parse_usd(value, allow_zero=allow_zero)


def parse_card_expiry(text: str) -> tuple[int, int]:
    if not text or "/" not in text:
        raise DomainError("有效期必须使用 MM/YYYY 格式")
    month_text, year_text = text.split("/", 1)
    if not _EXPIRY_MONTH.fullmatch(month_text.strip()):
        raise DomainError("有效期月份无效")
    year_text = year_text.strip()
    if not year_text.isdigit() or len(year_text) != 4:
        raise DomainError("有效期年份必须是四位数字")
    year = int(year_text)
    if not 2000 <= year <= 2100:
        raise DomainError("有效期年份无效")
    return int(month_text), year


def currency_is_usd(currency: str | None) -> bool:
    return isinstance(currency, str) and currency.strip().upper() == USD
