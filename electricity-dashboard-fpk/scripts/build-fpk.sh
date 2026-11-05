#!/bin/bash
# 用电总览 fpk 打包（Linux / fnOS 本机）
# 用法：
#   ./scripts/build-fpk.sh                       构建 manifest 当前版本的 x86 包
#   FPK_VERSION=1.0.1 ./scripts/build-fpk.sh     指定版本（同时改写 manifest 的 version）
#   FPK_PLATFORMS="x86 arm" ./scripts/build-fpk.sh  （arm 需先内置 arm wheels）
# 产物：dist-fpk/powerdash-<version>-<platform>.fpk 和 dist-fpk/SHA256SUMS.txt
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${FPK_OUT_DIR:-$ROOT_DIR/dist-fpk}"
MANIFEST="$ROOT_DIR/fpk/manifest"
MANIFEST_VERSION="$(awk -F '=' '/^version[[:space:]]*=/{gsub(/[[:space:]]/, "", $2); print $2}' "$MANIFEST")"
VERSION="${FPK_VERSION:-$MANIFEST_VERSION}"
PLATFORMS="${FPK_PLATFORMS:-x86}"

for platform in $PLATFORMS; do
  case "$platform" in
    x86|arm|loongarch|risc-v|all) ;;
    *) echo "不支持的平台：$platform（可选 x86、arm、loongarch、risc-v、all）" >&2; exit 1 ;;
  esac
done

if [ -n "${FNPACK_BIN:-}" ]; then
  FNPACK="$FNPACK_BIN"
elif command -v fnpack >/dev/null 2>&1; then
  FNPACK="$(command -v fnpack)"
else
  FNPACK="$(find "$ROOT_DIR" -maxdepth 1 -type f -name 'fnpack*' -perm -u+x 2>/dev/null | head -n 1 || true)"
fi
if [ -z "$FNPACK" ]; then
  echo "fnpack not found. Put fnpack in project root or set FNPACK_BIN=/abs/path/to/fnpack." >&2
  exit 1
fi

set_manifest() {
  local key="$1" value="$2"
  local padded
  padded="$(printf '%-22s' "$key")"
  sed -i.bak "s/^${key}[[:space:]]*=.*/${padded}= ${value}/" "$MANIFEST"
  rm -f "$MANIFEST.bak"
}

set_manifest version "$VERSION"
ORIGINAL_PLATFORM="$(awk -F '=' '/^platform[[:space:]]*=/{gsub(/[[:space:]]/, "", $2); print $2}' "$MANIFEST")"
trap 'set_manifest version "$MANIFEST_VERSION"; set_manifest platform "$ORIGINAL_PLATFORM"' EXIT

mkdir -p "$OUT_DIR"
BUILD_DIR="$(mktemp -d)"
trap 'set_manifest version "$MANIFEST_VERSION"; set_manifest platform "$ORIGINAL_PLATFORM"; rm -rf "$BUILD_DIR"' EXIT

outputs=()
for platform in $PLATFORMS; do
  set_manifest platform "$platform"
  (cd "$BUILD_DIR" && "$FNPACK" build --directory "$ROOT_DIR/fpk" >&2)
  built="$(find "$BUILD_DIR" "$ROOT_DIR/fpk" -maxdepth 1 -name '*.fpk' -print | head -n 1)"
  if [ -z "$built" ]; then
    echo "fnpack 完成但没有找到 $platform 平台的 .fpk 文件" >&2
    exit 1
  fi
  target="$OUT_DIR/powerdash-$VERSION-$platform.fpk"
  mv "$built" "$target"
  outputs+=("$target")
done

(
  cd "$OUT_DIR"
  names=()
  for path in "${outputs[@]}"; do names+=("$(basename "$path")"); done
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${names[@]}" > "SHA256SUMS.txt"
  else
    shasum -a 256 "${names[@]}" > "SHA256SUMS.txt"
  fi
)

for path in "${outputs[@]}"; do echo "$path"; done
