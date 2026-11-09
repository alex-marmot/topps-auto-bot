$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "此脚本必须在 Windows x64 上运行。"
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

& $python -c "import sys, tkinter; assert sys.version_info[:2] == (3, 14), sys.version; print('Python', sys.version.split()[0], 'Tk', tkinter.TkVersion)"
if ($LASTEXITCODE -ne 0) { throw "需要 CPython 3.14 与 Tk。" }

& $python -m pip install --require-hashes -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败。" }

& $python -m PyInstaller --noconfirm --clean packaging\topps_auto_bot.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }

$forbidden = @("config.json", "run-state.json", "browser-profile", "drivers", "driver-staging", "logs", "screenshots")
$dist = Join-Path $root "dist\ToppsAutoBot"
foreach ($name in $forbidden) {
    $found = Get-ChildItem -Path $dist -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $name }
    if ($found) { throw "安装包收集目录包含禁止的用户数据：$name" }
}

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "未找到 Inno Setup 6 的 ISCC.exe。" }

$version = (Get-Content pyproject.toml | Select-String -Pattern '^version = "([^"]+)"$').Matches.Groups[1].Value
& $iscc "/DAppVersion=$version" packaging\windows-installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 构建失败。" }

Write-Host "构建完成。安装包位于 installer-output\。未配置代码签名证书时，该产物为未签名版本。"
