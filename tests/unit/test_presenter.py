from __future__ import annotations

from datetime import datetime, timedelta

from tests.support import make_config, make_preset, window
from topps_auto_bot.models import PreparedLevel, ScheduleMode, ScheduleSettings, UTC, parse_beijing_datetime
from topps_auto_bot.presenter import (
    LOCAL_STORAGE_WARNING,
    SHIPPING_POLICY_TEXT,
    masked_card_fields,
    phase_label,
    preset_label,
    readiness_text,
    start_summary,
)
from topps_auto_bot.models import TaskPhase


def test_immediate_summary_before_ready_has_no_deadline() -> None:
    config = make_config()
    now = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    summary = start_summary(config, now=now)
    assert "准备就绪后开始" in summary["time_summary"]
    assert summary["window"].started_at_utc is None
    assert summary["window"].deadline_utc is None
    assert summary["target"] == preset_label(config.presets[0])
    assert summary["order_limit_usd"] == "USD 70.00"
    assert summary["shipping_policy"] == SHIPPING_POLICY_TEXT
    assert summary["local_storage_warning"] == LOCAL_STORAGE_WARNING


def test_old_scheduled_config_is_not_executed_in_baseline() -> None:
    start = parse_beijing_datetime("2026-11-07 09:30:00")
    config = make_config(
        schedule=ScheduleSettings(mode=ScheduleMode.SCHEDULED, scheduled_start_utc=start, window_seconds=120)
    )
    summary = start_summary(config, now=datetime(2026, 11, 6, 6, 0, tzinfo=UTC))
    assert "准备就绪后开始" in summary["time_summary"]
    assert summary["window"].deadline_utc is None
    assert summary["mode_text"] == "完整自动下单（立即执行，无整单截止）"
    assert "不会自动执行" in summary["notice"]


def test_active_immediate_summary_uses_actual_start() -> None:
    config = make_config()
    now = datetime(2026, 11, 6, 6, 2, 10, tzinfo=UTC)
    active = window(start=now, seconds=60)
    summary = start_summary(config, now=now + timedelta(seconds=10), window=active, ready_level=PreparedLevel.CHECKOUT_READY)
    assert "实际起点" in summary["time_summary"]
    assert "提交截止" in summary["time_summary"]
    assert summary["window"].remaining_seconds == 50


def test_summary_only_uses_selected_target_and_masks_card() -> None:
    a = make_preset(preset_id="a")
    b = make_preset(preset_id="b", title="Other Preset", variant="Retail Box")
    config = make_config(presets=(a, b), selected="a")
    summary = start_summary(config, now=datetime(2026, 11, 6, 6, 0, tzinfo=UTC))
    assert "Other Preset" not in summary["target"]
    assert summary["other_presets"] == [preset_label(b)]
    assert summary["card"]["number"].endswith("1111")
    assert summary["card"]["cvv"] == "***"
    assert "4111111111111111" not in str(summary)


def test_readiness_and_phase_labels() -> None:
    assert "未锁定库存" in readiness_text(PreparedLevel.CHECKOUT_READY)
    assert "未锁定库存" in readiness_text(PreparedLevel.CART_READY)
    assert phase_label(TaskPhase.WAITING_WINDOW) == "等待提交窗口"
    fields = masked_card_fields(make_config())
    assert fields["expiry"] == "12/2030"
