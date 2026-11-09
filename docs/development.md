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

在 Windows x64、已安装 Python 3.14 x64/Tk 和 Inno Setup 6.3+ 的环境中：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

脚本使用含 Windows 条件依赖的 `requirements-build.txt` 校验哈希。收集后执行包内 `--check-runtime` 验证 Tcl/Tk 和 UC 导入，再调用 Inno Setup。产物分别位于 `dist/ToppsAutoBot/` 和 `installer-output/ToppsAutoBot-Setup-<版本>.exe`。脚本校验安装包的 PE 产品版本，并生成同名 `.exe.sha256`。

发布构建需传入发布者提供的固定 HTTPS 清单地址，使用 `-Release -PublisherSource $PublisherSource`。清单为 UTF-8 JSON，包含三段数字的 `version`、纯文本 `notes` 和同源 HTTPS `download_url`。构建会校验响应，地址随包固化。开发构建允许省略该参数。

### GitHub Actions 构建与产物

在仓库 **Settings → Actions → General** 启用 Actions，允许工作流中列出的 GitHub 官方 actions。将工作流提交到仓库后，面向 `main` 的 PR、`main` push 及 **Actions → Windows CI → Run workflow** 都会触发构建。工作流使用 `windows-2025` x64、Python 3.14 x64/Tk、锁定依赖及 Inno Setup 6.7.4。ChromeDriver 由 `scripts/fetch_test_driver.py` 按当前 Chrome 版本准备。

在对应运行的 **Artifacts** 下载以下产物。

- `windows-x64-installer` 包含安装包和 SHA-256 文件。测试失败或跳过会阻止打包。
- `windows-x64-diagnostics` 包含阶段结果、仅含用例名称和结果的测试报告（省略参数值及失败详情），以及已执行阶段的依赖、驱动和构建日志。测试未启动时，阶段结果会显示原因所在步骤。测试只使用合成资料，产物上传路径限于这些文件。

### 首次配置 Pages 和发布

1. 在 **Settings → Pages** 将 Source 设为 **GitHub Actions**，确认站点可匿名访问。在 **Settings → Secrets and variables → Actions → Variables** 创建仓库变量 `PUBLISHER_SOURCE`，填实际 HTTPS 站点根 URL 加 `/latest.json`。工作流用 Pages 配置返回的实际地址校验该变量。保持固定 URL，已安装客户端会继续使用构建时固化的地址。
2. 在 **Settings → Environments** 为 `windows-release` 和 `github-pages` 配置允许部署的分支与所需审批。本流程从 `main` 手动触发。Windows 构建作业只有 `contents: read`，发布作业使用 `contents: write`，Pages 部署作业使用 `pages: write` 和 `id-token: write`。仓库策略须允许对应授权。
3. 首次在 **Actions → Windows Release and Update Pages → Run workflow** 选择 `bootstrap`，留空 tag。仅在固定清单返回 404 时部署一个合规的 `0.0.0` 清单和明确说明“尚未发布”的同源页面。确认匿名访问清单成功后再做正式构建。后续版本跳过此步。
4. 在通过普通用户桌面验收的对应提交创建版本 tag，格式为 `vX.Y.Z`。核对 `pyproject.toml` 与程序版本，从 `main` 手动选择 `release`，填 tag 和发行说明。该作业会复用 Windows CI，传入 `-Release -PublisherSource`，校验 tag、版本、安装包及 SHA-256，然后把资产和说明放入草稿，上传完成才发布。上传中断留下草稿时，先检查草稿及现有资产，再处理该草稿并重跑。
5. Release 发布后，手动选择 `pages`，填写同一个 tag。作业匿名读取该正式 Release，下载并核对公开安装包与 SHA-256。验证通过后在同一次 Pages 部署中切换 `latest.json` 和 `download.html`。清单仍由项目生成，下载说明页链接到对应 Release。发布、资产验证或 Pages 部署失败时，保留现有更新入口。Release 已发布但 Pages 未切换时，可单独重跑 `pages`。

后续版本从步骤 4 开始。恢复上一更新清单时，用 `pages` 选择仍可公开下载且校验通过的旧 tag。保留旧版 Release 和校验文件。已安装新版的客户端会显示发布源版本较旧，回退安装由用户按安装说明操作。

Windows CI 可在私有仓库运行。此发布工作流要求公开分发仓库。私有仓库的 Release 资产需要有仓库访问权的用户登录 GitHub，即使 Pages 对外可见，也无法为普通用户提供匿名下载。需要匿名分发时，在获准的公开分发仓库配置 Release 和 Pages。客户端只请求公开 HTTPS 清单，不携带 GitHub token。

官方资料见 [Windows 托管 runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)和 [Release 匿名访问条件](https://docs.github.com/en/rest/releases/releases#get-a-release-by-tag-name)。

### 普通用户桌面验收

GitHub 托管 runner 有管理员权限和预装工具。其测试和冻结程序检查与普通用户桌面验收分别记录。当前工作流不执行安装器。

在一台无 Python 的 Windows x64 普通用户机器上，使用合成资料验证以下路径。

- 安装并从开始菜单启动。自动下载、取消后手动导入、沙箱参数变更后重验、可见 UC 与 profile 重启保持。
- 使用受控页面验证人工登录、单目标持续等待、加购重试、真实跨端口 iframe 填写、银行验证及未决恢复。
- 安装旧版并准备合成配置、profile、驱动和未决记录。旧程序运行时覆盖须被阻断。正常退出后演练覆盖、取消、注入安装写入失败并回退，核对程序可恢复、用户文件保持以及新版不自动恢复任务。
- 演练卸载时分别保留和清除合成资料。记录签名状态、各组件版本及最终包清单。

重新生成锁文件时，对 `requirements.in`、`requirements-dev.in` 和 `requirements-build.in` 使用 `uv pip compile --universal --python-version 3.14 --generate-hashes`。UC 3.5.5 从 `distutils.version` 导入 `LooseVersion`，Python 3.14 通过已锁定的 setuptools 提供该兼容模块。PyInstaller 收集定义须保留这一运行依赖。

## 代码边界

- UI 只处理表单与展示；后台线程通过队列向 UI 发送状态，不直接调用 Tk 控件。
- `browser.py` 负责可信来源、Selenium/UC 页面适配与 JS 普通控件兜底；`purchase.py` 负责内容核对、配送快照和提交记录；`runner.py` 负责任务生命周期、持续等待、分步超时、取消和时序。
- 购买流程使用可见官网页面。软件更新通过独立的只读 HTTPS 请求获取公开发行信息。
