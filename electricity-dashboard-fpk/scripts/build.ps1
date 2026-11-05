# 用电总览 fpk 打包（Windows）
# 用法：.\scripts\build.ps1                # 构建 x86（arm 离线依赖未内置）
#       .\scripts\build.ps1 -Platform arm  # 需要 arm 时（需先内置 arm wheels）
param(
    [string]$Platform = "x86"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$fpkDir = Join-Path $root "fpk"
$dist = Join-Path $root "dist-fpk"
$manifest = Join-Path $fpkDir "manifest"

# 定位 fnpack：PATH > 项目根 > 脚本目录 > 环境变量 FNPACK_BIN
$fnpack = $env:FNPACK_BIN
if (-not $fnpack) { $fnpack = (Get-Command fnpack -ErrorAction SilentlyContinue).Source }
if (-not $fnpack) { $cand = @((Join-Path $root "fnpack.exe"), (Join-Path $PSScriptRoot "fnpack.exe")) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1; if ($cand) { $fnpack = $cand } }
if (-not $fnpack) { throw "fnpack not found. 下载官方 fnpack（Windows）放到项目根目录，或设置 FNPACK_BIN。" }

New-Item -ItemType Directory -Force -Path $dist | Out-Null

function Set-Platform([string]$p) {
    $t = [IO.File]::ReadAllText($manifest)
    $t = $t -replace '(?m)^platform[ \t]*=.*$', ("platform              = " + $p)
    [IO.File]::WriteAllText($manifest, $t, (New-Object Text.UTF8Encoding $false))
}

$version = ([regex]::Match([IO.File]::ReadAllText($manifest), '(?m)^version\s*=\s*(\S+)')).Groups[1].Value
if (-not $version) { throw "manifest 缺少 version" }

foreach ($p in ($Platform -split "\s+")) {
    Set-Platform $p
    Push-Location $dist
    try {
        & $fnpack build --directory $fpkDir
        if ($LASTEXITCODE -ne 0) { throw "fnpack build ($p) failed, exit=$LASTEXITCODE" }
    } finally { Pop-Location }
    $out = Join-Path $dist "powerdash.fpk"
    if (Test-Path -LiteralPath $out) {
        Move-Item -LiteralPath $out -Destination (Join-Path $dist "powerdash-$version-$p.fpk") -Force
    }
}
Set-Platform "x86"  # 还原默认 platform，避免把临时值留到 fpk 目录

Get-FileHash -Algorithm SHA256 -Path (Join-Path $dist "powerdash-$version-*.fpk") | ForEach-Object {
    "$($_.Hash.ToLower())  $([IO.Path]::GetFileName($_.Path))"
} | Set-Content (Join-Path $dist "SHA256SUMS.txt")

Write-Host "打包完成，产物在 $dist"
