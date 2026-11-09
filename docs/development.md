# 开发与测试

## 环境基线

- CPython 3.14.x（本项目在 macOS 上使用 uv 管理的 CPython 3.14.0 + Tk 8.6 验证）。
- Chrome 与匹配的 ChromeDriver。
- Windows 10/11 x64 是首个交付目标；macOS 结果不能代替 Windows 验收。

macOS 上若 Homebrew 的 `python@3.14` 缺少 `_tkinter`，使用 uv 提供的 Python：

```sh
uv python install 3.14.0
uv venv --python 3.14.0 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Windows 使用官方 Python 3.14 安装包（勾选 Tcl/Tk）或等价的 uv Python；在 PowerShell 中：

```powershell
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
```

所有命令都在本仓库根目录执行，不进入外层 ERP/Nuxt 目录。

## 常用命令

```sh
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m pytest tests/integration
.venv/bin/python -m pytest tests/integration/test_full_flow_local.py
.venv/bin/python -m pytest
.venv/bin/python -m topps_auto_bot --data-dir /tmp/topps-auto-bot-dev
```

Windows 对应 `.venv\Scripts\python.exe`。`--data-dir` 只用于开发和测试隔离；正常使用使用当前用户数据目录。

检查环境（Python、Tk、Selenium、Chrome）：

```sh
.venv/bin/python scripts/check_environment.py
```

集成测试需要本机 Chrome 与匹配驱动。先显式准备测试驱动（会写入 `~/.cache/topps-auto-bot-test/`，不进入仓库）：

```sh
.venv/bin/python scripts/fetch_test_driver.py
```

没有驱动时，集成测试会跳过，不得把跳过说成通过。

## Tk 启动检查

在可用图形会话中运行：

```sh
.venv/bin/python -c "import tkinter as tk; r=tk.Tk(); r.withdraw(); r.update(); print('Tk', tk.TkVersion); r.destroy()"
```

本项目已验证 CPython 3.14.0 + Tk 8.6 可以创建并销毁窗口。Windows 仍需在目标机重复验证。

## OpenSpec 与代理技能

- 规划和规格共用 `openspec/`，不维护两份规格。
- Codex 技能在 `.agents/skills/`，Kimi Code 技能在 `.kimi-code/skills/`；两者当前按 `.gitignore` 仅本地保存。
- 新环境重新生成：

```sh
openspec init --tools codex,kimi --profile core --no-animation
```

该命令会生成代理技能文件；执行前核对影响范围，不修改无关全局配置。

规划检查：

```sh
openspec list --json
openspec status --change phase-0-topps-website-bot --json
openspec validate phase-0-topps-website-bot --strict --no-interactive
git diff --check
```

## Windows 构建

在 Windows x64、已安装 Python 3.14/Tk、PyInstaller 和 Inno Setup 6 的环境中：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

产物在 `dist/` 和 `installer-output/`。构建前会使用 `packaging/topps_auto_bot.spec` 的显式收集清单，不打包整个工作目录。真实配置、会话、日志、驱动和测试驱动不得进入安装包。

## 代码边界

- UI 只处理表单与展示；后台线程通过队列向 UI 发送状态，不直接调用 Tk 控件。
- `browser.py` 负责可信来源、Selenium/UC 页面适配与 JS 普通控件兜底；`purchase.py` 负责内容核对、配送快照和提交记录；`runner.py` 负责任务生命周期、持续等待、分步超时、取消和时序。
- 不新增自动加购、清空购物车、独立 HTTP 下单、Server Action 重放、验证码绕过或 `--no-sandbox`。
