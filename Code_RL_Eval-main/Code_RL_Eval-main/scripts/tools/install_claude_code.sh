#!/bin/bash
#
# 在 Docker (Linux) 容器内安装 Claude Code CLI
#
# 用法（在 Dockerfile 中）：
#   COPY tools/install_claude_code.sh /tmp/install_claude_code.sh
#   RUN bash /tmp/install_claude_code.sh && rm -f /tmp/install_claude_code.sh
#
# 说明：
#   - 使用官方原生安装器（无需 Node.js），安装到 ~/.local/bin/claude
#   - 兼容 debian/ubuntu(apt) 与 alpine(apk)
#   - API Key 通过运行时环境变量 ANTHROPIC_API_KEY 注入，脚本不做任何硬编码
#
set -euo pipefail

echo "=================================================="
echo "安装 Claude Code CLI (Linux)"
echo "=================================================="

HOME="${HOME:-/root}"
export HOME

# ------------------------------------------------------------------
# 0. 清除代理环境变量
#    Docker Desktop 可能把宿主机代理（如局域网上的 clash）注入到构建环境，
#    该地址在容器内不可达。本脚本全部使用国内源（淘宝 npm），无需任何代理。
#    这里 unset 后，子进程（apt/curl/npm）都不会再走那个不可达的代理。
# ------------------------------------------------------------------
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true

# ------------------------------------------------------------------
# 1. 安装系统依赖
#    原生安装器需要 curl 下载，运行时需要 ripgrep 和 libstdc++
#    如果设置了 SKIP_APT_INSTALL=1，则跳过（假定依赖已在 Dockerfile 中安装）
# ------------------------------------------------------------------
IS_ALPINE=0
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
    IS_ALPINE=1
    apk add --no-cache curl ca-certificates ripgrep libgcc libstdc++
else
    echo "⚠️  未识别的包管理器，假定 curl/ripgrep 已存在"
fi

# ------------------------------------------------------------------
# 2. 安装 Claude Code
#    CLAUDE_INSTALL_METHOD=npm  → 使用 npm + 淘宝镜像源（国内推荐，无需代理）
#    默认（native）              → 官方原生安装器，失败时回退到 npm
# ------------------------------------------------------------------
INSTALL_METHOD="${CLAUDE_INSTALL_METHOD:-native}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

install_via_npm() {
    if ! command -v npm >/dev/null 2>&1; then
        echo "❌ 未找到 npm，无法通过 npm 安装 claude-code" >&2
        return 1
    fi
    # 双保险：清空 npm 可能继承的代理配置（国内源无需代理）
    npm config delete proxy 2>/dev/null || true
    npm config delete https-proxy 2>/dev/null || true
    echo "--> 使用 npm 安装 @anthropic-ai/claude-code (registry: $NPM_REGISTRY)"
    npm install -g @anthropic-ai/claude-code \
        --registry="$NPM_REGISTRY" \
        --noproxy="*"
}

if [ "$INSTALL_METHOD" = "npm" ]; then
    # 国内环境：直接用 npm 淘宝源，避免访问 claude.ai
    install_via_npm
else
    echo "--> 下载并运行官方安装器: https://claude.ai/install.sh"
    if ! curl -fsSL https://claude.ai/install.sh | bash; then
        echo "⚠️  原生安装器失败，尝试 npm 回退..."
        install_via_npm || {
            echo "❌ 原生安装失败且 npm 回退失败，安装终止" >&2
            exit 1
        }
    fi
fi

# ------------------------------------------------------------------
# 3. 软链到全局 PATH，容器内任意工作目录都能直接调用 claude
# ------------------------------------------------------------------
CLAUDE_BIN=""
for candidate in "$HOME/.local/bin/claude" "/usr/local/bin/claude" "$(command -v claude 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        CLAUDE_BIN="$candidate"
        break
    fi
done

if [ -z "$CLAUDE_BIN" ]; then
    echo "❌ 安装后未找到 claude 可执行文件" >&2
    exit 1
fi

if [ "$CLAUDE_BIN" != "/usr/local/bin/claude" ]; then
    ln -sf "$CLAUDE_BIN" /usr/local/bin/claude
fi

# ------------------------------------------------------------------
# 4. Alpine/musl 环境需要关闭内置 ripgrep，改用系统 ripgrep
# ------------------------------------------------------------------
if [ "$IS_ALPINE" = "1" ]; then
    mkdir -p "$HOME/.claude"
    settings="$HOME/.claude/settings.json"
    if [ ! -f "$settings" ]; then
        echo '{ "env": { "USE_BUILTIN_RIPGREP": "0" } }' > "$settings"
    fi
fi

# ------------------------------------------------------------------
# 5. 验证
# ------------------------------------------------------------------
export PATH="$HOME/.local/bin:$PATH"
echo "--> 验证安装:"
claude --version

echo "=================================================="
echo "✅ Claude Code CLI 安装完成"
echo "   路径: $(command -v claude)"
echo "   无头运行示例: claude -p \"...\" --dangerously-skip-permissions"
echo "   （运行时通过 -e ANTHROPIC_API_KEY=sk-xxx 注入密钥）"
echo "=================================================="
