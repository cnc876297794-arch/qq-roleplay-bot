#!/bin/bash
# =============================================================================
# QQ Roleplay Bot - NapCat 安装脚本
# =============================================================================
# 从 NapCat 官方仓库下载 Linux 预编译的 QQ 客户端，
# 配合 start_bot.sh 即可启动整个机器人。
# 参考：https://github.com/NapNeko/NapCatQQ
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NAPCAT_DIR="$SCRIPT_DIR/napcat"
NAPCAT_TARBALL="napcat.linux.tar.gz"
NAPCAT_RELEASE_URL="${NAPCAT_RELEASE_URL:-https://github.com/NapNeko/NapCatQQ/releases/latest/download/napcat.linux.tar.gz}"

echo "[install] 准备 NapCat 安装..."
echo "[install] 目标目录: $NAPCAT_DIR"
echo "[install] 下载源: $NAPCAT_RELEASE_URL"

mkdir -p "$NAPCAT_DIR"

if [ -x "$NAPCAT_DIR/launch.sh" ] || [ -x "$NAPCAT_DIR/QQ" ]; then
    echo "[install] ✅ 已检测到现有 NapCat，跳过下载"
    exit 0
fi

if command -v curl >/dev/null 2>&1; then
    curl -L -f -o "$NAPCAT_DIR/$NAPCAT_TARBALL" "$NAPCAT_RELEASE_URL"
elif command -v wget >/dev/null 2>&1; then
    wget -O "$NAPCAT_DIR/$NAPCAT_TARBALL" "$NAPCAT_RELEASE_URL"
else
    echo "[install] ❌ 需要 curl 或 wget"
    exit 1
fi

echo "[install] 解压..."
tar -xzf "$NAPCAT_DIR/$NAPCAT_TARBALL" -C "$NAPCAT_DIR"
rm -f "$NAPCAT_DIR/$NAPCAT_TARBALL"

echo "[install] ✅ NapCat 安装完成"
echo "[install] 下一步："
echo "    1. cd $NAPCAT_DIR && ./launch.sh   # 首次启动扫码登录"
echo "    2. 参考 README.md 启动 Bot"
