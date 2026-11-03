#!/bin/bash
#
# 在 Docker (Linux) 容器内安装 OpenAI Codex CLI
#
# 用法（在 Dockerfile 中）：
#   COPY install_codex.sh /tmp/install_codex.sh
#   RUN SKIP_APT_INSTALL=1 bash /tmp/install_codex.sh && rm -f /tmp/install_codex.sh
#
# 说明：
#   - 通过 npm + 淘宝镜像源安装 @openai/codex（国内推荐，无需代理）
#   - 依赖 Node.js/npm（应已在 Dockerfile 中安装）
#   - API Key 通过运行时环境变量注入，脚本不做任何硬编码
#
set -euo pipefail

echo "=================================================="
echo "安装 OpenAI Codex CLI (Linux)"
echo "=================================================="

HOME="${HOME:-/root}"
export HOME

# ------------------------------------------------------------------
# 0. 清除代理环境变量
#    Docker Desktop 可能把宿主机代理（如局域网上的 clash）注入到构建环境，
#    该地址在容器内不可达。本脚本全部使用国内源（淘宝 npm），无需任何代理。
# ------------------------------------------------------------------
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true

# ------------------------------------------------------------------
# 1. 安装系统依赖
#    如果设置了 SKIP_APT_INSTALL=1，则跳过（假定依赖已在 Dockerfile 中安装）
# ------------------------------------------------------------------
if [ "${SKIP_APT_INSTALL:-0}" = "1" ]; then
    echo "--> 跳过系统依赖安装（SKIP_APT_INSTALL=1）"
elif command -v apt-get >/dev/null 2>&1; then
    echo "--> 使用 apt 安装依赖"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        curl ca-certificates ripgrep libstdc++6
    rm -rf /var/lib/apt/lists/*
elif command -v apk >/dev/null 2>&1; then
    echo "--> 使用 apk 安装依赖"
    apk add --no-cache curl ca-certificates ripgrep libgcc libstdc++
else
    echo "⚠️  未识别的包管理器，假定 curl/ripgrep 已存在"
fi

# ------------------------------------------------------------------
# 2. 通过 npm 安装 Codex CLI（淘宝镜像源，无需代理）
# ------------------------------------------------------------------
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

if ! command -v npm >/dev/null 2>&1; then
    echo "❌ 未找到 npm，无法通过 npm 安装 @openai/codex" >&2
    exit 1
fi

# 双保险：清空 npm 可能继承的代理配置（国内源无需代理）
npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true

echo "--> 使用 npm 安装 @openai/codex (registry: $NPM_REGISTRY)"
npm install -g @openai/codex \
    --registry="$NPM_REGISTRY" \
    --noproxy="*"

# ------------------------------------------------------------------
# 3. 软链到全局 PATH，容器内任意工作目录都能直接调用 codex
# ------------------------------------------------------------------
CODEX_BIN=""
for candidate in "$HOME/.local/bin/codex" "/usr/local/bin/codex" "$(command -v codex 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        CODEX_BIN="$candidate"
        break
    fi
done

if [ -z "$CODEX_BIN" ]; then
    echo "❌ 安装后未找到 codex 可执行文件" >&2
    exit 1
fi

if [ "$CODEX_BIN" != "/usr/local/bin/codex" ]; then
    ln -sf "$CODEX_BIN" /usr/local/bin/codex
fi

# ------------------------------------------------------------------
# 4. 验证
# ------------------------------------------------------------------
export PATH="$HOME/.local/bin:$PATH"
echo "--> 验证安装:"
codex --version

echo "=================================================="
echo "✅ OpenAI Codex CLI 安装完成"
echo "   路径: $(command -v codex)"
echo "   无头运行示例: codex exec \"...\" --dangerously-bypass-approvals-and-sandbox"
echo "=================================================="
