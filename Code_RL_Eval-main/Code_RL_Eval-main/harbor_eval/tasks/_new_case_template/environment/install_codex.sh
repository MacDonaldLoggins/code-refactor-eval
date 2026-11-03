#!/bin/bash
set -euo pipefail

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true

NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
CODEX_VERSION="${CODEX_VERSION:-0.145.0}"

npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true
npm install -g "@openai/codex@${CODEX_VERSION}" \
    --registry="${NPM_REGISTRY}" \
    --noproxy="*"

codex --version

