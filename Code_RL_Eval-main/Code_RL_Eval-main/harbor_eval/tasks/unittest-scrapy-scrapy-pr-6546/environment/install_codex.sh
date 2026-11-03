#!/bin/bash
set -euo pipefail

echo "=================================================="
echo "Installing OpenAI Codex CLI (Linux)"
echo "=================================================="

HOME="${HOME:-/root}"
export HOME

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true

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
    apk add --no-cache curl ca-certificates ripgrep libgcc libstdc++
else
    echo "WARNING: Unknown package manager; assuming curl/ripgrep already exist"
fi

NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
CODEX_VERSION="${CODEX_VERSION:-0.145.0}"

if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: npm not found; cannot install @openai/codex via npm" >&2
    exit 1
fi

npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true

echo "--> Installing @openai/codex@$CODEX_VERSION with npm (registry: $NPM_REGISTRY)"
npm install -g "@openai/codex@$CODEX_VERSION" \
    --registry="$NPM_REGISTRY" \
    --noproxy="*"

CODEX_BIN=""
for candidate in "$HOME/.local/bin/codex" "/usr/local/bin/codex" "$(command -v codex 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        CODEX_BIN="$candidate"
        break
    fi
done

if [ -z "$CODEX_BIN" ]; then
    echo "ERROR: codex executable not found after installation" >&2
    exit 1
fi

if [ "$CODEX_BIN" != "/usr/local/bin/codex" ]; then
    ln -sf "$CODEX_BIN" /usr/local/bin/codex
fi

export PATH="$HOME/.local/bin:$PATH"
echo "--> Verifying installation:"
codex --version

CODEX_CONFIG_SRC="${CODEX_CONFIG_SRC:-/tmp/codex_config.toml}"
if [ -f "$CODEX_CONFIG_SRC" ]; then
    mkdir -p "$HOME/.codex"
    cp "$CODEX_CONFIG_SRC" "$HOME/.codex/config.toml"
    echo "--> Wrote codex config: $HOME/.codex/config.toml (provider=xxx)"
else
    echo "WARNING: Codex config source not found: $CODEX_CONFIG_SRC; codex will use default config" >&2
fi

echo "=================================================="
echo "OpenAI Codex CLI installation complete"
echo "   Path: $(command -v codex)"
echo "   Headless example: codex exec \"...\" --dangerously-bypass-approvals-and-sandbox"
echo "=================================================="
