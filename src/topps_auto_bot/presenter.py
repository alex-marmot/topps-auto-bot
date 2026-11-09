from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import (
    ActiveWindow,
    AppConfig,
    PreparedLevel,
    ProductPreset,
    ScheduleMode,
    ScheduleSettings,
    TaskPhase,
    format_usd,
)
from .redaction import mask_card_number, mask_cvv
from .runner import WindowSummary, describe_window

SHIPPING_POLICY_TEXT = (
    "配送策略：只沿用官网唯一明确选中的默认配送服务；确认后服务身份或 USD 运费发生变化即停止，"
    "不会自动改选最低价、最快或其他配送服务。"
)

LOCAL_STORAGE_WARNING = (
    "信用卡资料按你的选择保存在当前用户的本地配置文件中。本地文件保存不等于加密，"
    "请保护电脑账户和配置文件，不要上传或分发。"
)


def preset_label(preset: ProductPreset) -> str:
    return f"{preset.title} / {preset.variant} × {preset.quantity} @ {format_usd(preset.unit_price_limit_usd)}"


def preset_rows(config: AppConfig | None) -> list[dict[str, Any]]:
    if config is None:
        return []
    return [
        {
            "id": preset.preset_id,
            "label": preset_label(preset),
            "selected": preset.preset_id == config.selected_preset_id,
            "title": preset.title,
            "variant": preset.variant,
            "url": preset.url,
            "quantity": preset.quantity,
            "unit_price_limit_usd": f"{preset.unit_price_limit_usd:.2f}",
        }
        for preset in config.presets
    ]


def masked_card_fields(config: AppConfig) -> dict[str, str]:
    return {
        "number": mask_card_number(config.card.number),
        "cvv": mask_cvv(config.card.cvv),
        "holder_name": config.card.holder_name,
        "expiry": f"{config.card.expiry_month:02d}/{config.card.expiry_year}",
    }


def baseline_schedule(schedule: ScheduleSettings) -> ScheduleSettings:
    return ScheduleSettings(
        mode=ScheduleMode.IMMEDIATE,
        scheduled_start_utc=None,
        window_seconds=schedule.window_seconds,
    )


def start_summary(
    config: AppConfig,
    *,
    now: datetime,
    window: ActiveWindow | None = None,
    ready_level: PreparedLevel = PreparedLevel.NONE,
    ready: bool = False,
) -> dict[str, Any]:
    preset = config.selected_preset()
    baseline = baseline_schedule(config.schedule)
    window_summary = describe_window(
        baseline,
        now=now,
        window=window,
        ready=ready or ready_level is not PreparedLevel.NONE,
    )
    if preset is None:
        target_text = "尚未选择唯一目标"
    else:
        target_text = preset_label(preset)
    return {
        "target": target_text,
        "order_limit_usd": format_usd(config.order_limit_usd),
        "schedule_mode": str(baseline.mode),
        "mode_text": "完整自动下单（立即执行，无整单截止）",
        "window_seconds": baseline.window_seconds,
        "time_summary": window_summary.text,
        "window": window_summary,
        "shipping_policy": SHIPPING_POLICY_TEXT,
        "local_storage_warning": LOCAL_STORAGE_WARNING,
        "card": masked_card_fields(config),
        "other_presets": [
            preset_label(preset)
            for preset in config.presets
            if preset.preset_id != config.selected_preset_id
        ],
        "ready_level": str(ready_level),
        "ready": ready or ready_level is not PreparedLevel.NONE,
        "notice": (
            "旧配置中的定时计划不会自动执行；当前基线必须由用户明确立即开始。"
            if config.schedule.mode is ScheduleMode.SCHEDULED
            else ""
        ),
    }


def phase_label(phase: TaskPhase) -> str:
    return {
        TaskPhase.IDLE: "空闲",
        TaskPhase.PREPARING: "准备中",
        TaskPhase.AUTO_LOGIN: "自动登录中",
        TaskPhase.AWAITING_MANUAL_LOGIN: "等待人工登录",
        TaskPhase.AWAITING_LOGIN: "等待登录",
        TaskPhase.RUN_READY: "运行已就绪",
        TaskPhase.OPENING_PRODUCT: "打开商品页",
        TaskPhase.WAITING_AVAILABILITY: "等待可购",
        TaskPhase.ADDING_TO_CART: "加购处理中",
        TaskPhase.VERIFYING_CART: "核对购物车",
        TaskPhase.CART_READY: "购物车已准备",
        TaskPhase.CHECKOUT_READY: "结算页已准备",
        TaskPhase.WAITING_WINDOW: "等待提交窗口",
        TaskPhase.CHECKING_OUT: "进入结算",
        TaskPhase.REVALIDATING: "重新核对",
        TaskPhase.SUBMITTING: "购买已提交",
        TaskPhase.NOT_SUBMITTED: "未提交",
        TaskPhase.AWAITING_VERIFICATION: "等待结果/本人验证",
        TaskPhase.SUCCEEDED: "订单已确认",
        TaskPhase.FAILED: "失败",
        TaskPhase.UNKNOWN: "结果不明",
        TaskPhase.STOPPING: "停止处理中",
        TaskPhase.STOPPED: "已停止",
        TaskPhase.EXPIRED: "提交窗口已截止",
        TaskPhase.MISSED: "已错过/未就绪",
        TaskPhase.UNSUPPORTED: "当前不支持",
        TaskPhase.BLOCKED: "未决付款阻断",
    }.get(phase, str(phase))


def readiness_text(level: PreparedLevel) -> str:
    return {
        PreparedLevel.NONE: "尚未准备就绪",
        PreparedLevel.CART_READY: "购物车已准备；税费、运费、总额和付款表单仍需在结算页核对；未锁定库存",
        PreparedLevel.CHECKOUT_READY: "结算页已准备；未下单、未付款、未锁定库存",
    }[level]


def format_remaining(summary: WindowSummary) -> str:
    if summary.remaining_seconds is None:
        return ""
    return f"剩余 {int(summary.remaining_seconds)} 秒"
