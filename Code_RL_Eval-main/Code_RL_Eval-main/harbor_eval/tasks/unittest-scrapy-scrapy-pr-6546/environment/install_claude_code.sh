#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "Installing Claude Code CLI (Linux)"
echo "=================================================="

HOME="${HOME:-/root}"
export HOME

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true

IS_ALPINE=0
if [ "${SKIP_APT_INSTALL:-0}" = "1" ]; then
    echo "--> Skipping system dependency installation (SKIP_APT_INSTALL=1)"
elif command -v apt-get >/dev/null 2>&1; then
    echo "--> Installing dependencies with apt"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        curl ca-certificates ripgrep libstdc++6
    rm -rf /var/lib/apt/lists/*
elif command -v apk >/dev/null 2>&1; then
    echo "--> Installing dependencies with apk"
    IS_ALPINE=1
    apk add --no-cache curl ca-certificates ripgrep libgcc libstdc++
else
    echo "WARNING: Unknown package manager; assuming curl/ripgrep already exist"
fi

INSTALL_METHOD="${CLAUDE_INSTALL_METHOD:-native}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

install_via_npm() {
    if ! command -v npm >/dev/null 2>&1; then
        echo "ERROR: npm not found; cannot install claude-code via npm" >&2
        return 1
    fi
    npm config delete proxy 2>/dev/null || true
    npm config delete https-proxy 2>/dev/null || true
    echo "--> Installing @anthropic-ai/claude-code with npm (registry: $NPM_REGISTRY)"
    npm install -g @anthropic-ai/claude-code \
        --registry="$NPM_REGISTRY" \
        --noproxy="*"
}

if [ "$INSTALL_METHOD" = "npm" ]; then
    install_via_npm
else
    echo "--> Downloading and running official installer: https://claude.ai/install.sh"
    if ! curl -fsSL https://claude.ai/install.sh | bash; then
        echo "WARNING: Native installer failed; trying npm fallback..."
        install_via_npm || {
            echo "ERROR: Native installation and npm fallback both failed" >&2
            exit 1
        }
    fi
fi

CLAUDE_BIN=""
for candidate in "$HOME/.local/bin/claude" "/usr/local/bin/claude" "$(command -v claude 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        CLAUDE_BIN="$candidate"
        break
    fi
done

if [ -z "$CLAUDE_BIN" ]; then
    echo "ERROR: claude executable not found after installation" >&2
    exit 1
fi

if [ "$CLAUDE_BIN" != "/usr/local/bin/claude" ]; then
    ln -sf "$CLAUDE_BIN" /usr/local/bin/claude
fi

if [ "$IS_ALPINE" = "1" ]; then
    mkdir -p "$HOME/.claude"
    settings="$HOME/.claude/settings.json"
    if [ ! -f "$settings" ]; then
        echo '{ "env": { "USE_BUILTIN_RIPGREP": "0" } }' > "$settings"
    fi
fi

export PATH="$HOME/.local/bin:$PATH"
echo "--> Verifying installation:"
claude --version

echo "=================================================="
echo "Claude Code CLI installation complete"
echo "   Path: $(command -v claude)"
echo "   Headless example: claude -p \"...\" --dangerously-skip-permissions"
echo "   Inject the key at runtime with -e ANTHROPIC_API_KEY=sk-xxx"
echo "=================================================="
