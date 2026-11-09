# 功能参考、独立实现边界与依赖来源

## 功能研究参考

- 参考项目：`pythonicshariful/purchase-atomation-topps.com`，审查提交 `7a123e13241167d4e92d66ff03a17205363c996e`。
- 该仓库仅用于了解用户希望解决的直购流程、常见错误案例和功能范围，不是本项目的源码基线或依赖。
- 本项目未导入、复制或逐句翻译其源码、注释、文档、资源和配置样例。实现依据本仓库 OpenSpec 规格、官方库文档和独立核实的网页行为。
- 已阅读过参考项目源码这一事实如实保留；本项目不宣称严格净室开发，也不宣称零版权风险。若以后复用任何第三方具体代码或资源，必须先重新确认许可。

## 实际使用的依赖

运行与测试依赖以带 SHA-256 校验值的锁定文件为准：

- `requirements.txt` / `requirements-dev.txt` / `requirements-build.txt`
- 输入文件：`requirements.in`、`requirements-dev.in`、`requirements-build.in`
- 生成方式：`uv pip compile <input> --generate-hashes -o <output>`

| 依赖 | 锁定版本 | 用途 | 许可/来源 |
| --- | --- | --- | --- |
| CPython | 3.14.0（macOS 开发验证） | 运行与测试 | Python Software Foundation License；源码与官方安装包见 https://www.python.org/ |
| Tcl/Tk | 8.6（uv Python 3.14.0 内置） | Tkinter UI | BSD 风格许可；见 Tcl/Tk 官方发行说明 |
| Selenium | 4.52.0 | 浏览器自动化 | Apache-2.0；https://pypi.org/project/selenium/ |
| undetected-chromedriver | 3.5.5 | UC 浏览器启动兼容 | GPL-3.0；https://pypi.org/project/undetected-chromedriver/ |
| pytest | 8.4.2 | 测试 | MIT；https://pypi.org/project/pytest/ |
| PyInstaller | 6.17.0 | Windows 打包 | GPL-2.0-or-later，含 bootloader exception；https://pyinstaller.org/ |
| Inno Setup | 6.x（Windows 构建时确认实际版本） | Windows 用户级安装器 | Inno Setup 官方许可（可免费分发；以构建时官方许可文本为准）；https://jrsoftware.org/isinfo.php |
| Google Chrome / ChromeDriver | 开发验证为 156.0.8078.80 | 浏览器与驱动 | Google Chrome for Testing 官方元数据与下载；https://googlechromelabs.github.io/chrome-for-testing/ |
| uv | 开发工具 | 虚拟环境与依赖锁定 | MIT/Apache-2.0；不进入运行分发包 |

## 官方文档

- ChromeDriver 版本选择：https://developer.chrome.com/docs/chromedriver/downloads/version-selection
- undetected-chromedriver：https://github.com/ultrafunkamsterdam/undetected-chromedriver
- Selenium Manager 边界（本项目不依赖其隐式下载）：https://www.selenium.dev/documentation/selenium_manager/
- Chrome for Testing：https://googlechromelabs.github.io/chrome-for-testing/
- PyInstaller：https://pyinstaller.org/en/stable/
- Python 版本状态：https://devguide.python.org/versions/
- Windows 用户数据目录：https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid

## 独立实现检查

- 应用源码位于 `src/topps_auto_bot/`，未包含参考项目的文件或资源。
- 配置样例 `config.example.json` 由本项目编写，仅含空值/占位值，不包含真实个人或支付资料。
- 研究阶段只读观察过公开页面脚本和参考项目提交；没有把公开脚本中的内部动作当作可直接重放的接口。
- 真实官网验证、Windows 构建和实际付款验收仍以 `docs/validation.md` 中记录的完成状态为准。
