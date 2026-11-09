from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
import uuid
import webbrowser
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .browser import (
    DEFAULT_TOPPs_PROFILE,
    SeleniumPageAdapter,
    TrustedSourcePolicy,
    open_live_session,
    verify_blank_browser,
)
from .driver_setup import (
    CfTClient,
    DriverOperation,
    DriverOperationCancelled,
    DriverSetupError,
    DriverValidationError,
    DownloadProgress,
    check_enabled_driver,
    current_platform_tag,
    detect_chrome,
    inspect_chrome,
    patch_driver_for_uc,
    prepare_automatic_driver,
    prepare_manual_driver,
)
from .models import (
    AddToCartState,
    AppConfig,
    Card,
    Customer,
    DEFAULT_WINDOW_SECONDS,
    DriverSettings,
    LoginCredentials,
    PreparedLevel,
    ProductPreset,
    ScheduleMode,
    ScheduleSettings,
    SubmissionState,
    TargetSnapshot,
    TaskEvent,
    ReadStatus,
    TaskPhase,
    TaskSnapshot,
    parse_quantity,
    parse_usd,
)
from .presenter import (
    LOCAL_STORAGE_WARNING,
    SHIPPING_POLICY_TEXT,
    baseline_schedule,
    masked_card_fields,
    phase_label,
    preset_label,
    readiness_text,
    start_summary,
)
from .state import RunStateStatus, RunStateStore
from .redaction import Redactor, build_diagnostic_report, mask_card_number, mask_cvv, mask_password
from .runner import (
    ActiveWindow,
    FullFlowRunner,
    SingleInstanceLock,
    SystemClock,
    TaskManager,
    describe_window,
    new_task_id,
)
from .settings import (
    AppPaths,
    CURRENT_SCHEMA_VERSION,
    ConfigErrorKind,
    ConfigSaveError,
    config_with_driver,
    default_paths,
    load_config,
    parse_card_expiry,
    replace_invalid_config,
    save_config,
    validate_config,
)


def configure_tcl_environment() -> None:
    """Make Tk find its data files when running from a venv built on standalone Python."""
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return
    base = Path(sys.base_prefix) / "lib"
    for name in ("tcl9.0", "tcl8.6", "tcl8"):
        candidate = base / name
        if candidate.is_dir():
            os.environ.setdefault("TCL_LIBRARY", str(candidate))
            break
    for name in ("tk9.0", "tk8.6", "tk8"):
        candidate = base / name
        if candidate.is_dir():
            os.environ.setdefault("TK_LIBRARY", str(candidate))
            break


class ToppsAutoBotApp:
    def __init__(self, root: tk.Tk, paths: AppPaths | None = None) -> None:
        self.root = root
        self.paths = paths or default_paths()
        self.paths.ensure_directories()
        self.clock = SystemClock()
        self.events: "queue.Queue[TaskEvent]" = queue.Queue()
        self.driver_events: "queue.Queue[tuple]" = queue.Queue()
        self.store = RunStateStore(self.paths.run_state)
        self.manager = TaskManager(
            store=self.store,
            events=self.events,
            policy=TrustedSourcePolicy(),
            runner_factory=FullFlowRunner,
        )
        self.loaded = load_config(self.paths.config)
        self.config: AppConfig | None = self.loaded.config
        self.session: SeleniumPageAdapter | None = None
        self.current_phase = TaskPhase.IDLE
        self.active_window: ActiveWindow | None = None
        self.ready_level = PreparedLevel.NONE
        self._prepared_level = PreparedLevel.NONE
        self.run_ready = False
        self.driver_chrome = None
        self.driver_operation: DriverOperation | None = None
        self._presets: list[ProductPreset] = list(self.config.presets) if self.config else []
        self._selected_preset_id = self.config.selected_preset_id if self.config else None
        self._direct_target: ProductPreset | None = None
        self._card_number_actual = self.config.card.number if self.config else ""
        self._card_cvv_actual = self.config.card.cvv if self.config else ""
        self._login_password_actual = self.config.login.password if self.config else ""
        self._card_number_masked = mask_card_number(self._card_number_actual) if self.config else ""
        self._card_cvv_masked = mask_cvv(self._card_cvv_actual) if self.config else ""
        self._login_password_masked = mask_password(self._login_password_actual) if self.config else ""
        self.pending_var = tk.StringVar(value="")
        self._event_log: list[dict] = []
        self._build_variables()
        self._build_ui()
        self._load_into_form()
        self._refresh_all()
        self._refresh_pending()
        self.root.after(100, self._poll_events)
        self.root.after(1000, self._tick_runtime)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_variables(self) -> None:
        config = self.config
        self.status_var = tk.StringVar(value="")
        self.phase_var = tk.StringVar(value=phase_label(TaskPhase.IDLE))
        self.readiness_var = tk.StringVar(value=readiness_text(PreparedLevel.NONE))
        self.time_var = tk.StringVar(value="")
        self.target_var = tk.StringVar(value="")
        self.driver_status_var = tk.StringVar(value="尚未检测 Chrome")
        self.progress_var = tk.StringVar(value="")
        self.login_email_var = tk.StringVar(value=config.login.email if config else "")
        self.login_password_var = tk.StringVar(value=self._login_password_masked)
        self.name_var = tk.StringVar(value=config.customer.full_name if config else "")
        self.email_var = tk.StringVar(value=config.customer.email if config else "")
        self.phone_var = tk.StringVar(value=config.customer.phone if config else "")
        self.address1_var = tk.StringVar(value=config.customer.address_line1 if config else "")
        self.address2_var = tk.StringVar(value=config.customer.address_line2 if config else "")
        self.city_var = tk.StringVar(value=config.customer.city if config else "")
        self.region_var = tk.StringVar(value=config.customer.region if config else "")
        self.postal_var = tk.StringVar(value=config.customer.postal_code if config else "")
        self.country_var = tk.StringVar(value=config.customer.country if config else "US")
        self.card_number_var = tk.StringVar(value=self._card_number_masked)
        self.card_cvv_var = tk.StringVar(value=self._card_cvv_masked)
        self.card_holder_var = tk.StringVar(value=config.card.holder_name if config else "")
        self.card_expiry_var = tk.StringVar(
            value=f"{config.card.expiry_month:02d}/{config.card.expiry_year}" if config else ""
        )
        self.schedule_mode_var = tk.StringVar(value=str(ScheduleMode.IMMEDIATE))
        self.start_at_var = tk.StringVar(value="")
        self.window_var = tk.StringVar(
            value=str(config.schedule.window_seconds) if config else "60"
        )
        self.order_limit_var = tk.StringVar(
            value=f"{config.order_limit_usd:.2f}" if config else ""
        )
        self.preset_title_var = tk.StringVar()
        self.preset_variant_var = tk.StringVar()
        self.preset_url_var = tk.StringVar()
        self.preset_quantity_var = tk.StringVar(value="1")
        self.preset_price_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.search_result_var = tk.StringVar()
        self.show_card_var = tk.BooleanVar(value=False)
        self.show_login_var = tk.BooleanVar(value=False)
        self.no_sandbox_var = tk.BooleanVar(value=config.driver.no_sandbox if config else False)
        self.search_candidates = {}

    def _build_ui(self) -> None:
        self.root.title("Topps Auto Bot — 限量直购")
        self.root.geometry("980x760")
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.settings_tab = ttk.Frame(notebook)
        self.task_tab = ttk.Frame(notebook)
        self.driver_tab = ttk.Frame(notebook)
        notebook.add(self.settings_tab, text="设置")
        notebook.add(self.task_tab, text="任务")
        notebook.add(self.driver_tab, text="驱动")
        self._build_settings_tab()
        self._build_task_tab()
        self._build_driver_tab()
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w")
        status_bar.pack(fill="x", padx=10, pady=(0, 8))

    def _build_settings_tab(self) -> None:
        tab = self.settings_tab
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="本地配置文件：" + str(self.paths.config)).pack(side="left")
        ttk.Label(top, text=LOCAL_STORAGE_WARNING, foreground="#8a4b00", wraplength=880).pack(
            side="left", padx=12
        )

        presets = ttk.LabelFrame(tab, text="商品预设（可保存多个，本次只执行一个）")
        presets.pack(fill="both", expand=True, padx=6, pady=4)
        self.preset_tree = ttk.Treeview(presets, columns=("selected", "label"), show="headings", height=5)
        self.preset_tree.heading("selected", text="本次")
        self.preset_tree.heading("label", text="商品 / 规格 / 数量 / 单价上限")
        self.preset_tree.column("selected", width=60, anchor="center")
        self.preset_tree.column("label", width=820)
        self.preset_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.preset_tree.bind("<<TreeviewSelect>>", self._on_preset_select)
        editor = ttk.Frame(presets)
        editor.pack(fill="x", padx=4, pady=4)
        for index, (label, variable, width) in enumerate(
            (
                ("名称", self.preset_title_var, 24),
                ("规格", self.preset_variant_var, 16),
                ("链接", self.preset_url_var, 40),
                ("数量", self.preset_quantity_var, 6),
                ("单价上限 USD", self.preset_price_var, 10),
            )
        ):
            ttk.Label(editor, text=label).grid(row=0, column=index * 2, sticky="e", padx=(6 if index else 0, 2))
            ttk.Entry(editor, textvariable=variable, width=width).grid(row=0, column=index * 2 + 1, sticky="we")
        buttons = ttk.Frame(presets)
        buttons.pack(fill="x", padx=4, pady=4)
        ttk.Button(buttons, text="新增/更新预设", command=self._upsert_preset).pack(side="left")
        ttk.Button(buttons, text="删除所选预设", command=self._delete_preset).pack(side="left", padx=6)
        ttk.Button(buttons, text="设为本次唯一目标", command=self._select_preset).pack(side="left")
        ttk.Button(buttons, text="使用当前输入（不保存）", command=self._use_editor_target).pack(side="left", padx=6)
        ttk.Label(buttons, text="整单上限 USD（含税和运费）").pack(side="left", padx=(20, 4))
        ttk.Entry(buttons, textvariable=self.order_limit_var, width=10).pack(side="left")
        search_row = ttk.Frame(presets)
        search_row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Label(search_row, text="官网搜索关键词").pack(side="left")
        ttk.Entry(search_row, textvariable=self.search_var, width=24).pack(side="left", padx=4)
        ttk.Button(search_row, text="搜索候选", command=self._search_targets).pack(side="left")
        ttk.Button(search_row, text="读取商品链接", command=self._read_product_link).pack(side="left", padx=6)
        self.search_combo = ttk.Combobox(search_row, textvariable=self.search_result_var, state="readonly", width=54)
        self.search_combo.pack(side="left", padx=6)
        self.search_combo.bind("<<ComboboxSelected>>", self._apply_search_candidate)

        login_frame = ttk.LabelFrame(tab, text="登录资料（先尝试自动登录，失败或验证码转同会话人工）")
        login_frame.pack(fill="x", padx=6, pady=4)
        ttk.Label(login_frame, text="登录邮箱").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(login_frame, textvariable=self.login_email_var, width=32).grid(
            row=0, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Label(login_frame, text="密码（如官网需要）").grid(row=0, column=2, sticky="e", padx=4)
        ttk.Entry(login_frame, textvariable=self.login_password_var, width=22).grid(
            row=0, column=3, sticky="w", padx=4, pady=2
        )
        ttk.Checkbutton(
            login_frame, text="显示/编辑密码", variable=self.show_login_var, command=self._toggle_login
        ).grid(row=0, column=4, sticky="w", padx=8)
        ttk.Label(
            login_frame,
            text="登录账号与收货邮箱独立；密码按当前用户本地配置明文保存，不是加密。"
            "验证码不保存、不读取邮箱；自动登录失败或需要额外认证时请在专用 Chrome 中人工完成。",
            wraplength=900,
        ).grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=2)

        details = ttk.LabelFrame(tab, text="收货与信用卡资料（仅保存在本地）")
        details.pack(fill="x", padx=6, pady=4)
        fields = [
            ("收货人", self.name_var),
            ("邮箱", self.email_var),
            ("电话", self.phone_var),
            ("地址1", self.address1_var),
            ("地址2", self.address2_var),
            ("城市", self.city_var),
            ("州/省", self.region_var),
            ("邮编", self.postal_var),
            ("国家", self.country_var),
        ]
        for index, (label, variable) in enumerate(fields):
            row, column = divmod(index, 3)
            ttk.Label(details, text=label).grid(row=row, column=column * 2, sticky="e", padx=4, pady=2)
            ttk.Entry(details, textvariable=variable, width=28).grid(
                row=row, column=column * 2 + 1, sticky="we", padx=4, pady=2
            )
        card_row = ttk.Frame(details)
        card_row.grid(row=3, column=0, columnspan=6, sticky="we", pady=4)
        ttk.Label(card_row, text="卡号").pack(side="left", padx=4)
        ttk.Entry(card_row, textvariable=self.card_number_var, width=22).pack(side="left")
        ttk.Label(card_row, text="有效期 MM/YYYY").pack(side="left", padx=(12, 4))
        ttk.Entry(card_row, textvariable=self.card_expiry_var, width=10).pack(side="left")
        ttk.Label(card_row, text="CVV").pack(side="left", padx=(12, 4))
        ttk.Entry(card_row, textvariable=self.card_cvv_var, width=6).pack(side="left")
        ttk.Label(card_row, text="持卡人").pack(side="left", padx=(12, 4))
        ttk.Entry(card_row, textvariable=self.card_holder_var, width=20).pack(side="left")
        ttk.Checkbutton(card_row, text="显示/编辑", variable=self.show_card_var, command=self._toggle_card).pack(
            side="left", padx=12
        )

        timing = ttk.LabelFrame(tab, text="执行计划（基线仅立即执行）")
        timing.pack(fill="x", padx=6, pady=4)
        ttk.Radiobutton(
            timing, text="立即：准备就绪后开启窗口", variable=self.schedule_mode_var, value="immediate"
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Radiobutton(
            timing, text="定时（基线暂停）", variable=self.schedule_mode_var, value="scheduled", state="disabled"
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=4)
        ttk.Label(timing, text="旧定时时间（不会自动执行）").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(timing, textvariable=self.start_at_var, width=22, state="disabled").grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(
            timing,
            text="诊断兼容：显式关闭 Chrome 沙箱（默认不勾选，改变后需重新验证驱动/启动）",
            variable=self.no_sandbox_var,
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=4)
        ttk.Label(
            timing,
            text="当前基线按步骤超时和 Stop 控制，无整单截止或倒计时；加载旧窗口/定时字段不会调度或限制新任务。"
            + SHIPPING_POLICY_TEXT,
            wraplength=880,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=4)

        actions = ttk.Frame(tab)
        actions.pack(fill="x", padx=6, pady=6)
        ttk.Button(actions, text="保存设置", command=self._save_settings).pack(side="left")
        ttk.Button(actions, text="备份并重建配置", command=self._repair_config).pack(side="left", padx=6)
        ttk.Button(actions, text="打开专用 Chrome 会话", command=self._open_browser).pack(side="left", padx=6)
        ttk.Button(actions, text="开始完整流程", command=self._start_task).pack(side="left")
        ttk.Button(actions, text="停止", command=self._stop_task).pack(side="left", padx=6)

    def _build_task_tab(self) -> None:
        tab = self.task_tab
        summary = ttk.LabelFrame(tab, text="本次唯一目标与时间")
        summary.pack(fill="x", padx=6, pady=6)
        ttk.Label(summary, textvariable=self.target_var, wraplength=920).pack(anchor="w", padx=6, pady=2)
        ttk.Label(summary, textvariable=self.time_var, wraplength=920).pack(anchor="w", padx=6, pady=2)
        ttk.Label(summary, textvariable=self.readiness_var, wraplength=920).pack(anchor="w", padx=6, pady=2)
        ttk.Label(summary, textvariable=self.phase_var, font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w", padx=6, pady=2
        )
        buttons = ttk.Frame(summary)
        buttons.pack(anchor="w", padx=6, pady=4)
        ttk.Button(buttons, text="开始完整流程", command=self._start_task).pack(side="left")
        ttk.Button(buttons, text="停止", command=self._stop_task).pack(side="left", padx=6)
        ttk.Button(buttons, text="刷新摘要", command=self._refresh_time_summary).pack(side="left", padx=6)
        ttk.Button(buttons, text="导出脱敏诊断", command=self._export_diagnostics).pack(side="left")
        pending = ttk.LabelFrame(tab, text="未决加购与付款核对")
        pending.pack(fill="x", padx=6, pady=6)
        ttk.Label(pending, textvariable=self.pending_var, wraplength=920).pack(anchor="w", padx=6, pady=2)
        add_buttons = ttk.Frame(pending)
        add_buttons.pack(anchor="w", padx=6, pady=2)
        ttk.Label(add_buttons, text="加购核对：").pack(side="left")
        ttk.Button(add_buttons, text="已成功", command=lambda: self._resolve_add(AddToCartState.CONFIRMED)).pack(side="left")
        ttk.Button(add_buttons, text="明确失败", command=lambda: self._resolve_add(AddToCartState.FAILED)).pack(side="left", padx=4)
        ttk.Button(add_buttons, text="未形成加购", command=lambda: self._resolve_add(AddToCartState.NOT_SENT)).pack(side="left")
        pending_buttons = ttk.Frame(pending)
        pending_buttons.pack(anchor="w", padx=6, pady=2)
        ttk.Label(pending_buttons, text="付款核对：").pack(side="left")
        ttk.Button(pending_buttons, text="成功", command=lambda: self._resolve_pending(SubmissionState.SUCCEEDED)).pack(side="left")
        ttk.Button(pending_buttons, text="拒绝/失败", command=lambda: self._resolve_pending(SubmissionState.DECLINED)).pack(side="left", padx=4)
        ttk.Button(pending_buttons, text="未形成订单", command=lambda: self._resolve_pending(SubmissionState.NOT_SUBMITTED)).pack(side="left")
        ttk.Button(pending_buttons, text="打开官网订单页面", command=self._open_orders_page).pack(side="left", padx=6)
        ttk.Label(tab, text="状态记录（脱敏）").pack(anchor="w", padx=10, pady=(8, 2))
        self.log_text = tk.Text(tab, height=24, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_driver_tab(self) -> None:
        tab = self.driver_tab
        info = ttk.LabelFrame(tab, text="Chrome 与驱动")
        info.pack(fill="x", padx=6, pady=6)
        ttk.Label(info, textvariable=self.driver_status_var, wraplength=900).pack(anchor="w", padx=6, pady=4)
        ttk.Label(
            info,
            text="驱动必须经过私有暂存、完整验证和原子启用；暂存或未验证副本不能用于购买。",
            wraplength=900,
        ).pack(anchor="w", padx=6, pady=2)
        buttons = ttk.Frame(info)
        buttons.pack(anchor="w", padx=6, pady=6)
        ttk.Button(buttons, text="重新检测 Chrome", command=self._detect_chrome).pack(side="left")
        ttk.Button(buttons, text="手动选择 Chrome", command=self._choose_chrome).pack(side="left", padx=6)
        ttk.Button(buttons, text="自动下载匹配驱动", command=self._download_driver).pack(side="left", padx=6)
        ttk.Button(buttons, text="取消下载，改为手动选择", command=self._cancel_driver).pack(side="left")
        ttk.Button(buttons, text="手动选择 chromedriver", command=self._manual_driver).pack(side="left", padx=6)
        ttk.Label(info, textvariable=self.progress_var, wraplength=900).pack(anchor="w", padx=6, pady=4)

    def _load_into_form(self) -> None:
        self._refresh_preset_tree()
        self._refresh_time_summary()

    def _refresh_preset_tree(self) -> None:
        for item in self.preset_tree.get_children():
            self.preset_tree.delete(item)
        for preset in self._presets:
            marker = "★" if preset.preset_id == self._selected_preset_id else ""
            self.preset_tree.insert("", "end", iid=preset.preset_id, values=(marker, preset_label(preset)))

    def _on_preset_select(self, _event=None) -> None:
        selection = self.preset_tree.selection()
        if not selection:
            return
        preset = next((p for p in self._presets if p.preset_id == selection[0]), None)
        if preset is None:
            return
        self.preset_title_var.set(preset.title)
        self.preset_variant_var.set(preset.variant)
        self.preset_url_var.set(preset.url)
        self.preset_quantity_var.set(str(preset.quantity))
        self.preset_price_var.set(f"{preset.unit_price_limit_usd:.2f}")

    def _upsert_preset(self) -> None:
        try:
            quantity = parse_quantity(self.preset_quantity_var.get())
            price = parse_usd(self.preset_price_var.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("预设无效", str(exc))
            return
        selection = self.preset_tree.selection()
        preset_id = selection[0] if selection else uuid.uuid4().hex
        preset = ProductPreset(
            preset_id=preset_id,
            title=self.preset_title_var.get().strip(),
            variant=self.preset_variant_var.get().strip(),
            url=self.preset_url_var.get().strip(),
            quantity=quantity,
            unit_price_limit_usd=price,
        )
        existing = [p for p in self._presets if p.preset_id != preset_id]
        self._presets = existing + [preset]
        if self._selected_preset_id is None:
            self._selected_preset_id = preset_id
        self._refresh_preset_tree()
        self.preset_tree.selection_set(preset_id)
        self.status_var.set("预设已加入表单列表；保存设置后才会写入本地配置")

    def _delete_preset(self) -> None:
        selection = self.preset_tree.selection()
        if not selection:
            return
        preset_id = selection[0]
        self._presets = [p for p in self._presets if p.preset_id != preset_id]
        if self._selected_preset_id == preset_id:
            self._selected_preset_id = self._presets[0].preset_id if self._presets else None
        self._refresh_preset_tree()

    def _session_required(self):
        if self.session is None:
            messagebox.showerror("浏览器未打开", "请先打开专用 Chrome 会话；工具不会另开匿名会话")
            return None
        return self.session

    def _search_targets(self) -> None:
        session = self._session_required()
        if session is None:
            return
        keyword = self.search_var.get().strip()
        result = session.search_products(keyword)
        if result.status is not ReadStatus.OK:
            messagebox.showerror("搜索失败", result.error or "无法读取官方搜索结果")
            return
        self.search_candidates = {}
        labels = []
        for index, candidate in enumerate(result.candidates):
            label = f"{candidate.title} — {candidate.url}"
            self.search_candidates[label] = candidate
            labels.append(label)
        self.search_combo["values"] = labels
        if not labels:
            self.search_result_var.set("")
            messagebox.showinfo("没有候选", "没有找到可确认的候选；程序不会自动选择其他商品")
            return
        self.status_var.set(f"找到 {len(labels)} 个候选；请选择一个确认，仍不会自动加购")

    def _apply_search_candidate(self, _event=None) -> None:
        candidate = self.search_candidates.get(self.search_result_var.get())
        if candidate is None:
            return
        self.preset_title_var.set(candidate.title)
        self.preset_url_var.set(candidate.url)
        if len(candidate.variants) == 1:
            self.preset_variant_var.set(candidate.variants[0])
        else:
            self.preset_variant_var.set("")
        self.status_var.set("已填入候选；请确认具体规格、数量和单价后点击“新增/更新预设”")

    def _read_product_link(self) -> None:
        session = self._session_required()
        if session is None:
            return
        url = self.preset_url_var.get().strip()
        result = session.read_product(url or None)
        if result.status is not ReadStatus.OK:
            messagebox.showerror("读取失败", result.error or "无法读取商品页面")
            return
        self.preset_title_var.set(result.title or "")
        if len(result.variants) == 1:
            self.preset_variant_var.set(result.variants[0])
        elif result.variants:
            self.preset_variant_var.set("")
            self.status_var.set("商品有多个规格，请选择一个规格后再保存：" + "、".join(result.variants))
        else:
            self.preset_variant_var.set("单一商品")
            self.status_var.set("页面未提供规格选项，已填入“单一商品”；请确认后保存")
        self.preset_url_var.set(result.url or url)
        self.status_var.set("已读取商品身份和规格；保存预设不等于已加购或已就绪")

    def _select_preset(self) -> None:
        selection = self.preset_tree.selection()
        if not selection:
            messagebox.showerror("未选择", "请先选择一个预设")
            return
        self._selected_preset_id = selection[0]
        self._direct_target = None
        self._refresh_preset_tree()

    def _use_editor_target(self) -> None:
        try:
            quantity = parse_quantity(self.preset_quantity_var.get())
            price = parse_usd(self.preset_price_var.get())
            preset = ProductPreset(
                preset_id="direct-" + uuid.uuid4().hex,
                title=self.preset_title_var.get().strip(),
                variant=self.preset_variant_var.get().strip(),
                url=self.preset_url_var.get().strip(),
                quantity=quantity,
                unit_price_limit_usd=price,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("目标无效", str(exc))
            return
        validate_config(
            replace(
                self.config or self._build_config_from_form(),
                presets=(preset,),
                selected_preset_id=preset.preset_id,
            ),
            now=self.clock.now(),
            require_login=True,
        )
        self._direct_target = preset
        self._selected_preset_id = None
        self._refresh_preset_tree()
        self.status_var.set("已使用当前输入作为本次目标（不保存为预设）；保存设置不会改变该临时目标")

    def _toggle_login(self) -> None:
        if self.show_login_var.get():
            self.login_password_var.set(self._login_password_actual)
        else:
            self.login_password_var.set(self._login_password_masked)

    def _toggle_card(self) -> None:
        if self.show_card_var.get():
            self.card_number_var.set(self._card_number_actual)
            self.card_cvv_var.set(self._card_cvv_actual)
        else:
            self.card_number_var.set(self._card_number_masked)
            self.card_cvv_var.set(self._card_cvv_masked)

    def _build_config_from_form(self) -> AppConfig:
        try:
            order_limit = parse_usd(self.order_limit_var.get())
            month, year = parse_card_expiry(self.card_expiry_var.get())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)) from exc
        mode = ScheduleMode.IMMEDIATE
        try:
            legacy_window = int(self.window_var.get().strip())
        except (TypeError, ValueError):
            legacy_window = DEFAULT_WINDOW_SECONDS
        if not 1 <= legacy_window <= 600:
            legacy_window = DEFAULT_WINDOW_SECONDS
        schedule = ScheduleSettings(mode=mode, scheduled_start_utc=None, window_seconds=legacy_window)
        number = self.card_number_var.get().strip()
        cvv = self.card_cvv_var.get().strip()
        if number == self._card_number_masked:
            number = self._card_number_actual
        if cvv == self._card_cvv_masked:
            cvv = self._card_cvv_actual
        login_password = self.login_password_var.get()
        if login_password == self._login_password_masked:
            login_password = self._login_password_actual
        customer = Customer(
            full_name=self.name_var.get().strip(),
            email=self.email_var.get().strip(),
            phone=self.phone_var.get().strip(),
            address_line1=self.address1_var.get().strip(),
            address_line2=self.address2_var.get().strip(),
            city=self.city_var.get().strip(),
            region=self.region_var.get().strip(),
            postal_code=self.postal_var.get().strip(),
            country=self.country_var.get().strip(),
        )
        card = Card(
            number=number,
            expiry_month=month,
            expiry_year=year,
            cvv=cvv,
            holder_name=self.card_holder_var.get().strip(),
        )
        driver = replace(
            self.config.driver if self.config else DriverSettings(),
            no_sandbox=bool(self.no_sandbox_var.get()),
        )
        config = AppConfig(
            schema_version=CURRENT_SCHEMA_VERSION,
            presets=tuple(self._presets),
            selected_preset_id=self._selected_preset_id,
            order_limit_usd=order_limit,
            login=LoginCredentials(
                email=self.login_email_var.get().strip(),
                password=login_password,
            ),
            customer=customer,
            card=card,
            driver=driver,
            schedule=schedule,
        )
        validate_config(config, now=self.clock.now(), require_login=True)
        return config

    def _save_settings(self, *, silent: bool = False) -> bool:
        if self.manager.is_active():
            messagebox.showerror("任务运行中", "请先停止并等待当前任务结束，再修改交易设置")
            return False
        try:
            config = self._build_config_from_form()
            save_config(self.paths.config, config, now=self.clock.now())
        except (ValueError, ConfigSaveError) as exc:
            messagebox.showerror("保存失败", str(exc))
            return False
        self.loaded = load_config(self.paths.config)
        self.config = self.loaded.config
        self._card_number_actual = config.card.number
        self._card_cvv_actual = config.card.cvv
        self._card_number_masked = mask_card_number(config.card.number)
        self._card_cvv_masked = mask_cvv(config.card.cvv)
        self._login_password_actual = config.login.password
        self._login_password_masked = mask_password(config.login.password)
        if not self.show_card_var.get():
            self.card_number_var.set(self._card_number_masked)
            self.card_cvv_var.set(self._card_cvv_masked)
        if not self.show_login_var.get():
            self.login_password_var.set(self._login_password_masked)
        self.status_var.set("设置已保存；下一次任务会使用新配置")
        self._refresh_time_summary()
        if not silent:
            messagebox.showinfo("已保存", "设置已保存到当前用户的本地配置")
        return True

    def _repair_config(self) -> None:
        if self.manager.is_active():
            messagebox.showerror("任务运行中", "请先停止当前任务")
            return
        if not messagebox.askyesno(
            "备份并重建配置",
            "将把当前配置文件改名备份，然后写入界面中的设置。只有在原文件损坏或版本不兼容时才应这样做。继续？",
        ):
            return
        try:
            config = self._build_config_from_form()
            replace_invalid_config(self.paths.config, config)
        except (ValueError, ConfigSaveError) as exc:
            messagebox.showerror("重建失败", str(exc))
            return
        self.loaded = load_config(self.paths.config)
        self.config = self.loaded.config
        self._card_number_actual = config.card.number
        self._card_cvv_actual = config.card.cvv
        self._card_number_masked = mask_card_number(config.card.number)
        self._card_cvv_masked = mask_cvv(config.card.cvv)
        self._login_password_actual = config.login.password
        self._login_password_masked = mask_password(config.login.password)
        if not self.show_login_var.get():
            self.login_password_var.set(self._login_password_masked)
        self.status_var.set("配置已重建；原文件已备份")
        self._refresh_all()

    def _refresh_pending(self) -> None:
        loaded = self.store.load()
        if loaded.status is RunStateStatus.MISSING:
            self.pending_var.set("没有未决加购或购买提交")
            return
        if loaded.status is not RunStateStatus.OK:
            self.pending_var.set(
                f"运行状态不可用（{loaded.status}）：{loaded.error or ''}；已保留原记录并阻止新任务"
            )
            return
        parts: list[str] = []
        if loaded.add_to_cart is not None:
            add = loaded.add_to_cart
            if loaded.add_to_cart.state in (AddToCartState.IN_FLIGHT, AddToCartState.UNKNOWN):
                parts.append(
                    f"未决加购 {add.state}：任务 {add.task_id}，"
                    f"{add.target_title} / {add.target_variant} × {add.target_quantity}。"
                    "请先在官方购物车核对是否已有该商品，再显式记录结论；"
                    "停止、重启或一次空车读数都不能解除阻断。"
                )
            else:
                parts.append(f"最近加购记录：{add.state}")
        if loaded.purchase is not None:
            purchase = loaded.purchase
            if loaded.purchase.state in (SubmissionState.SUBMITTING, SubmissionState.UNKNOWN):
                parts.append(
                    f"未决付款 {purchase.state}：任务 {purchase.task_id}，"
                    f"{purchase.target_title} / {purchase.target_variant} × {purchase.target_quantity}，"
                    f"总额 USD {purchase.total_usd}。请先在官方渠道核对订单，再显式记录结论。"
                )
            else:
                parts.append(f"最近购买记录：{purchase.state}")
        self.pending_var.set("\n".join(parts) if parts else "没有未决加购或购买提交")

    def _resolve_pending(self, state: SubmissionState) -> None:
        if not messagebox.askyesno(
            "确认人工付款核对结论",
            "请确认你已在官网订单或支付渠道完成核对。该结论只解除本地付款阻断，不会恢复旧任务，也不会自动重新付款。",
        ):
            return
        try:
            self.store.resolve_purchase(
                state=state,
                note="用户在官方渠道核对后显式记录",
                now=self.clock.now(),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法记录结论", str(exc))
            return
        self._refresh_pending()
        self.status_var.set("已记录人工付款结论；旧任务保持结束，新任务必须重新准备")

    def _resolve_add(self, state: AddToCartState) -> None:
        if not messagebox.askyesno(
            "确认人工加购核对结论",
            "请确认你已在官方购物车核对旧加购结果。该结论只解除本地加购阻断，新任务仍必须从空购物车重新开始。",
        ):
            return
        try:
            self.store.resolve_add_to_cart(
                state=state,
                note="用户在官方购物车核对后显式记录",
                now=self.clock.now(),
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法记录结论", str(exc))
            return
        self._refresh_pending()
        self.status_var.set("已记录人工加购结论；旧任务保持结束，新任务仍需空车检查")

    def _open_orders_page(self) -> None:
        webbrowser.open("https://www.topps.com/")

    def _refresh_all(self) -> None:
        if self.loaded.status is ConfigErrorKind.MISSING:
            self.status_var.set("尚未配置：请填写设置；配置完成前不能开始购买任务")
        elif self.loaded.status:
            self.status_var.set(
                f"配置异常（{self.loaded.status}）：{self.loaded.error or ''}；原文件已保留，购买任务被阻止"
            )
        else:
            self.status_var.set("配置已加载；启动应用不会自动搜索、加购或付款")
        self._refresh_time_summary()
        self._update_start_button()

    def _refresh_time_summary(self) -> None:
        config = self.config
        if config is None:
            self.target_var.set("尚未选择唯一目标")
            self.time_var.set("")
            return
        summary = start_summary(
            config,
            now=self.clock.now(),
            window=self.active_window,
            ready=self.run_ready,
        )
        target_text = summary["target"]
        if self._direct_target is not None:
            target_text = "临时目标（不保存）：" + preset_label(self._direct_target)
        self.target_var.set(
            f"本次唯一目标：{target_text}；整单上限（含税运费）：{summary['order_limit_usd']}；"
            f"{summary['mode_text']}"
        )
        self.time_var.set(summary["time_summary"])
        if self.run_ready:
            self.readiness_var.set("运行已就绪；完整流程执行中，已加购不代表已付款或已锁定库存")
        elif not self.manager.is_active():
            self.readiness_var.set("尚未运行就绪；需要环境、登录、空车和未决检查通过")
        if summary["notice"]:
            self.status_var.set(summary["notice"])
        if self.session is not None and not DEFAULT_TOPPs_PROFILE.verified:
            self.status_var.set(
                "专用浏览器会话已打开；官网页面定位尚未完成第 0 组现场验证，自动推进被阻止"
            )

    def _update_start_button(self) -> None:
        pass

    def _open_browser(self) -> bool:
        if self.manager.is_active():
            messagebox.showerror("任务运行中", "不能在中途切换浏览器")
            return False
        if self.config is None:
            messagebox.showerror("配置未完成", "请先保存完整配置")
            return False
        chrome = self.driver_chrome or detect_chrome()
        if chrome is None:
            messagebox.showerror("未找到 Chrome", "请安装 Chrome，或在驱动页重新检测")
            return False
        self.driver_chrome = chrome
        check = check_enabled_driver(self.config.driver, chrome=chrome)
        if not check.ok:
            messagebox.showerror("驱动未就绪", check.error or "请先在驱动页准备驱动")
            return False
        try:
            self.session = open_live_session(
                chrome_path=str(chrome.path),
                driver_path=self.config.driver.path or "",
                profile_dir=self.paths.browser_profile,
                policy=TrustedSourcePolicy(),
                profile=DEFAULT_TOPPs_PROFILE,
                no_sandbox=self.config.driver.no_sandbox,
            )
            self.status_var.set(
                "专用 Chrome 会话已打开；程序会先复用配置账号会话，否则尝试一次自动登录，失败或验证码时转人工"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("浏览器启动失败", str(exc))
            return False

    def _start_task(self) -> None:
        if self.manager.is_active():
            messagebox.showerror("已有任务", "请等待当前任务结束或停止完成")
            return
        if self.config is None:
            messagebox.showerror("配置未完成", "请先保存完整配置")
            return
        if not DEFAULT_TOPPs_PROFILE.verified:
            messagebox.showerror(
                "官网页面定位未验证",
                "第 0 组实站验证尚未完成，不能用未验证的页面定位自动推进购买。"
                "已打开的专用浏览器会话仍可手动使用。",
            )
            return
        if self.session is None:
            if not self._open_browser():
                return
        blocked = self.store.load()
        if blocked.blocking:
            messagebox.showerror(
                "未决记录阻断",
                (blocked.error or "存在未决加购或购买提交")
                + "；请先在官方页面核对，不能直接开始新任务。",
            )
            return
        chrome = self.driver_chrome or detect_chrome()
        if chrome is None:
            messagebox.showerror("未找到 Chrome", "请先准备浏览器")
            return
        check = check_enabled_driver(self.config.driver, chrome=chrome)
        if not check.ok:
            messagebox.showerror("驱动未就绪", check.error or "请先准备驱动")
            return
        target = self.config.target_snapshot()
        if self._direct_target is not None:
            target = TargetSnapshot(preset=self._direct_target, order_limit_usd=self.config.order_limit_usd)
        if target is None:
            messagebox.showerror("目标不明确", "请选择预设或点击“使用当前输入（不保存）”")
            return
        if not self.config.login.email.strip():
            messagebox.showerror("登录资料缺失", "请先在设置中填写并保存登录邮箱；缺少资料不能开始自动登录")
            return
        snapshot = TaskSnapshot(
            task_id=new_task_id(),
            target=target,
            login=self.config.login,
            customer=self.config.customer,
            card=self.config.card,
            driver=self.config.driver,
            schedule=baseline_schedule(self.config.schedule),
            created_at_utc=self.clock.now(),
        )
        result = self.manager.start(snapshot, self.session)
        if not result.ok:
            messagebox.showerror("无法开始", result.message)
            return
        self.current_phase = TaskPhase.PREPARING
        self.run_ready = False
        self.active_window = None
        self.phase_var.set(phase_label(self.current_phase))
        self.status_var.set("任务已启动；会先核实配置账号登录，随后持续等待目标可购；可随时 Stop")
        self._refresh_time_summary()

    def _stop_task(self) -> None:
        if not self.manager.is_active():
            self.status_var.set("当前没有活动任务")
            return
        self.manager.stop()
        self.status_var.set("停止处理中；等待在途浏览器操作按有限超时退出")

    def _choose_chrome(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 Chrome 可执行文件",
            filetypes=[("Chrome", "chrome*"), ("Applications", "*.app"), ("All files", "*")],
        )
        if not selected:
            return
        try:
            chrome = inspect_chrome(Path(selected))
        except DriverSetupError as exc:
            messagebox.showerror("Chrome 无效", str(exc))
            return
        self.driver_chrome = chrome
        self.driver_status_var.set(f"Chrome：{chrome.path}（版本 {chrome.version}，{chrome.architecture}）")

    def _detect_chrome(self) -> None:
        chrome = detect_chrome()
        self.driver_chrome = chrome
        if chrome is None:
            self.driver_status_var.set("未检测到 Chrome；请安装后重新检测或手动选择浏览器")
        else:
            self.driver_status_var.set(f"Chrome：{chrome.path}（版本 {chrome.version}，{chrome.architecture}）")

    def _ensure_driver_context(self):
        if self.manager.is_active():
            messagebox.showerror("任务运行中", "不能在任务执行、等待或验证期间更换驱动")
            return None
        if self.driver_chrome is None:
            self._detect_chrome()
        if self.driver_chrome is None:
            messagebox.showerror("未找到 Chrome", "请先检测或选择 Chrome")
            return None
        return self.driver_chrome

    def _download_driver(self) -> None:
        if self.driver_operation is not None:
            messagebox.showerror("驱动操作进行中", "请等待当前驱动操作结束或先取消")
            return
        chrome = self._ensure_driver_context()
        if chrome is None:
            return
        operation = DriverOperation(operation_id=uuid.uuid4().hex)
        self.driver_operation = operation
        self.progress_var.set("正在读取官方版本元数据…")

        def progress(update: DownloadProgress) -> None:
            self.driver_events.put(("progress", update))

        def store_selection(settings) -> None:
            current = load_config(self.paths.config)
            if not current.ok:
                raise DriverValidationError("本地配置不可用，无法保存驱动选择")
            save_config(
                self.paths.config,
                config_with_driver(current.config, settings),
                now=self.clock.now(),
            )

        def worker() -> None:
            try:
                activated = prepare_automatic_driver(
                    chrome=chrome,
                    paths=self.paths,
                    operation=operation,
                    client=CfTClient(),
                    platform_tag=current_platform_tag(),
                    progress=progress,
                    browser_verifier=lambda **kwargs: verify_blank_browser(
                        **kwargs, no_sandbox=self.config.driver.no_sandbox
                    ),
                    driver_preparer=patch_driver_for_uc,
                    store_selection=store_selection,
                )
                self.driver_events.put(("done", activated))
            except DriverOperationCancelled:
                self.driver_events.put(("cancelled", "下载已取消"))
            except DriverSetupError as exc:
                self.driver_events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.driver_events.put(("error", f"驱动准备失败：{exc}"))

        threading.Thread(target=worker, name="topps-driver-download", daemon=True).start()

    def _cancel_driver(self) -> None:
        if self.driver_operation is None:
            return
        self.driver_operation.cancel()
        self.progress_var.set("已取消下载并撤销该操作的启用权限；可改为手动选择驱动")

    def _manual_driver(self) -> None:
        if self.driver_operation is not None:
            messagebox.showerror("驱动操作进行中", "请等待当前驱动操作结束或先取消")
            return
        chrome = self._ensure_driver_context()
        if chrome is None:
            return
        selected = filedialog.askopenfilename(title="选择解压后的 chromedriver", filetypes=[("chromedriver", "chromedriver*"), ("All files", "*")])
        if not selected:
            return
        operation = DriverOperation(operation_id=uuid.uuid4().hex)
        self.driver_operation = operation
        self.progress_var.set("正在复制到私有暂存区并验证…")

        def store_selection(settings) -> None:
            current = load_config(self.paths.config)
            if not current.ok:
                raise DriverValidationError("本地配置不可用，无法保存驱动选择")
            save_config(
                self.paths.config,
                config_with_driver(current.config, settings),
                now=self.clock.now(),
            )

        def worker() -> None:
            try:
                activated = prepare_manual_driver(
                    source=Path(selected),
                    chrome=chrome,
                    paths=self.paths,
                    operation=operation,
                    platform_tag=current_platform_tag(),
                    browser_verifier=lambda **kwargs: verify_blank_browser(
                        **kwargs, no_sandbox=self.config.driver.no_sandbox
                    ),
                    driver_preparer=patch_driver_for_uc,
                    store_selection=store_selection,
                )
                self.driver_events.put(("done", activated))
            except DriverOperationCancelled:
                self.driver_events.put(("cancelled", "操作已取消"))
            except DriverSetupError as exc:
                self.driver_events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.driver_events.put(("error", f"手动驱动准备失败：{exc}"))

        threading.Thread(target=worker, name="topps-driver-manual", daemon=True).start()

    def _tick_runtime(self) -> None:
        if self.manager.is_active() and self.active_window is not None:
            self._refresh_time_summary()
        self.root.after(1000, self._tick_runtime)

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._append_log(event)
                self.current_phase = event.phase
                self.phase_var.set(phase_label(event.phase))
                if event.phase is TaskPhase.RUN_READY:
                    self.run_ready = True
                    self.readiness_var.set("运行已就绪；从商品页开始完整流程，无整单截止")
                    started_text = event.details.get("started_at_utc")
                    if isinstance(started_text, str):
                        try:
                            started = datetime.fromisoformat(started_text)
                            self.active_window = ActiveWindow(
                                mode=ScheduleMode.IMMEDIATE,
                                started_at_utc=started,
                                effective_start_utc=started,
                                deadline_utc=None,
                            )
                        except ValueError:
                            self.active_window = None
                elif event.phase in (
                    TaskPhase.STOPPED,
                    TaskPhase.EXPIRED,
                    TaskPhase.MISSED,
                    TaskPhase.UNSUPPORTED,
                    TaskPhase.FAILED,
                    TaskPhase.UNKNOWN,
                    TaskPhase.SUCCEEDED,
                    TaskPhase.NOT_SUBMITTED,
                    TaskPhase.BLOCKED,
                ):
                    self.run_ready = False
                    self.readiness_var.set(f"任务结束：{phase_label(event.phase)}")
                    self._refresh_pending()
                self._refresh_time_summary()
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.driver_events.get_nowait()
                if kind == "progress":
                    update: DownloadProgress = payload
                    if update.total_bytes:
                        percent = int(update.downloaded_bytes * 100 / update.total_bytes)
                        self.progress_var.set(
                            f"{update.stage}：{percent}% ({update.downloaded_bytes}/{update.total_bytes} 字节)"
                        )
                    else:
                        self.progress_var.set(f"{update.stage}：已下载 {update.downloaded_bytes} 字节（无总长度）")
                elif kind == "done":
                    self.progress_var.set(f"驱动已启用：{payload.path}（版本 {payload.version}）")
                    self.loaded = load_config(self.paths.config)
                    self.config = self.loaded.config
                    self.driver_operation = None
                elif kind == "cancelled":
                    self.progress_var.set(str(payload) + "；未启用该副本，旧驱动选择保持不变")
                    self.driver_operation = None
                else:
                    self.progress_var.set(f"驱动准备失败：{payload}")
                    self.driver_operation = None
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _export_diagnostics(self) -> None:
        if self.config is None:
            messagebox.showerror("无法导出", "尚未加载有效配置")
            return
        target = filedialog.asksaveasfilename(
            title="导出脱敏诊断",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not target:
            return
        loaded = self.store.load()
        state_parts = []
        if loaded.add_to_cart is not None:
            state_parts.append(f"add_to_cart:{loaded.add_to_cart.state}")
        if loaded.purchase is not None:
            state_parts.append(f"purchase:{loaded.purchase.state}")
        report = build_diagnostic_report(
            redactor=Redactor.from_credentials(self.config.customer, self.config.card, self.config.login),
            phase=str(self.current_phase),
            events=self._event_log[-300:],
            submission_state=(",".join(state_parts) if state_parts else None),
            runtime={
                "python": sys.version.split()[0],
                "tk": str(tk.TkVersion),
                "chrome": self.driver_chrome.version if self.driver_chrome else None,
                "driver_version": self.config.driver.version,
                "driver_mode": self.config.driver.mode,
            },
            created_at=self.clock.now(),
        )
        try:
            Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            Path(target).chmod(0o600)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self.status_var.set("已导出脱敏诊断；不包含配置正文、会话或支付页面")

    def _append_log(self, event: TaskEvent) -> None:
        self._event_log.append(
            {
                "at_utc": event.at_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "phase": str(event.phase),
                "message": event.message,
                "details": event.details,
            }
        )
        timestamp = event.at_utc.astimezone(timezone.utc).strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {phase_label(event.phase)}：{event.message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.manager.is_active():
            if not messagebox.askyesno("任务仍在运行", "停止自动操作并关闭程序？未决付款仍需在官方渠道核对。"):
                return
            self.manager.stop()
            self.manager.join(timeout=5)
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


def run_ui(paths: AppPaths | None = None) -> None:
    configure_tcl_environment()
    root = tk.Tk()
    ToppsAutoBotApp(root, paths=paths)
    root.mainloop()
