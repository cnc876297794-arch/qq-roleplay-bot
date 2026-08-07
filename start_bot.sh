#!/bin/bash
# =============================================================================
# QQ Roleplay Bot - 启动脚本 (Linux)
# =============================================================================
# 用法：
#   1. 复制 .env.example 为 .env 并填入真实值
#   2. 复制 config/*.example 为去掉 .example 的文件并按需修改
#   3. ./start_bot.sh
# =============================================================================
set -euo pipefail

# ─── 定位项目根目录（脚本所在目录）───
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── 加载 .env（若存在）───
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
    echo "[start_bot] 已加载 .env"
else
    echo "[start_bot] 未找到 .env，将依赖系统环境变量"
fi

# ─── Python 虚拟环境（推荐，但非必需）───
if [ -d "$SCRIPT_DIR/venv" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/venv/bin/activate"
elif [ -d "$SCRIPT_DIR/.venv" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.venv/bin/activate"
fi

# ─── 前置检查 ───
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo "[start_bot] ⚠️  未设置 DEEPSEEK_API_KEY"
    echo "[start_bot]    请在 .env 中配置，或 export DEEPSEEK_API_KEY=sk-..."
    echo "[start_bot]    继续启动，但 AI 回复会失败"
fi

if [ ! -f "$SCRIPT_DIR/config/bot.yaml" ]; then
    echo "[start_bot] ❌ 缺少 config/bot.yaml"
    echo "[start_bot]    cp config/bot.yaml.example config/bot.yaml 后再试"
    exit 1
fi

# ─── 启动 NoneBot2 主程序 ───
mkdir -p "$SCRIPT_DIR/data"
LOG_FILE="${QQBOT_LOG_FILE:-$SCRIPT_DIR/bot.log}"

echo "[start_bot] 启动 NoneBot2，端口=${PORT:-8080}，日志=$LOG_FILE"
exec python "$SCRIPT_DIR/qqbot/bot.py"
