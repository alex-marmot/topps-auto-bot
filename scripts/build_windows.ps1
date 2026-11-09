param(
    [string]$PublisherSource = "",
    [switch]$Release
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "此脚本必须在 Windows x64 上运行。"
}
if ($Release -and -not $PublisherSource) {
    throw "发布构建必须配置固定 HTTPS 发布源。"
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    py -3.14 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "需要 CPython 3.14 x64。" }
}

& $python -c "import platform, sys, tkinter; assert sys.version_info[:2] == (3, 14); assert platform.machine() == 'AMD64'; print('Python', sys.version.split()[0], 'Tk', tkinter.TkVersion)"
if ($LASTEXITCODE -ne 0) { throw "需要 CPython 3.14 x64 与 Tk。" }

& $python -m pip install --require-hashes -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败。" }

$env:PYTHONPATH = Join-Path $root "src"
$resourceDir = Join-Path $root "build\publisher"
try {
    Remove-Item Env:\TOPPS_BUILD_PUBLISHER_RESOURCE -ErrorAction SilentlyContinue
    if ($PublisherSource) {
        & $python -c "import sys; from topps_auto_bot.updates import publisher_origin; publisher_origin(sys.argv[1])" $PublisherSource
        if ($LASTEXITCODE -ne 0) { throw "发布源必须是可信的 HTTPS URL。" }
        New-Item $resourceDir -ItemType Directory -Force | Out-Null
        $resource = Join-Path $resourceDir "publisher-update-source.txt"
        [IO.File]::WriteAllText($resource, $PublisherSource, [Text.UTF8Encoding]::new($false))
        $env:TOPPS_BUILD_PUBLISHER_RESOURCE = $resource
    }
    if ($Release) {
        & $python -c "import sys; from topps_auto_bot.updates import check_for_update; check_for_update(sys.argv[1])" $PublisherSource
        if ($LASTEXITCODE -ne 0) { throw "固定发布源未通过 HTTPS 响应检查。" }
    }

    & $python -m PyInstaller --noconfirm --clean packaging\topps_auto_bot.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }
} finally {
    Remove-Item Env:\TOPPS_BUILD_PUBLISHER_RESOURCE -ErrorAction SilentlyContinue
    Remove-Item $resourceDir -Recurse -Force -ErrorAction SilentlyContinue
}

$dist = Join-Path $root "dist\ToppsAutoBot"
$runtime = Start-Process -FilePath (Join-Path $dist "ToppsAutoBot.exe") -ArgumentList "--check-runtime" -Wait -PassThru
if ($runtime.ExitCode -ne 0) { throw "打包后的 Tk/UC 运行时导入失败。" }

& $python scripts\check_release_hygiene.py
if ($LASTEXITCODE -ne 0) { throw "安装包收集目录或仓库卫生检查失败。" }

$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue)
if ($iscc) {
    $iscc = $iscc.Source
} else {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "未找到 Inno Setup 6 的 ISCC.exe。" }

$version = & $python -c "import tomllib; from topps_auto_bot import __version__; p = tomllib.load(open('pyproject.toml', 'rb')); assert p['project']['version'] == __version__; from topps_auto_bot.updates import _version; _version(__version__); print(__version__)"
if ($LASTEXITCODE -ne 0) { throw "项目和程序的版本不一致。" }
$installer = Join-Path $root "installer-output\ToppsAutoBot-Setup-$version.exe"
Remove-Item -LiteralPath $installer, "$installer.sha256" -ErrorAction SilentlyContinue
& $iscc "/DAppVersion=$version" packaging\windows-installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 构建失败。" }

$asset = Get-Item -LiteralPath $installer
$info = $asset.VersionInfo
if ($asset.Length -le 0 -or "$($info.ProductMajorPart).$($info.ProductMinorPart).$($info.ProductBuildPart)" -ne $version -or $info.ProductPrivatePart -ne 0) {
    throw "安装包的 PE 产品版本与程序版本不一致。"
}
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $($asset.Name)" | Set-Content -LiteralPath "$installer.sha256" -Encoding ascii

Write-Host "构建完成。安装包位于 installer-output\，为未签名构建。"
