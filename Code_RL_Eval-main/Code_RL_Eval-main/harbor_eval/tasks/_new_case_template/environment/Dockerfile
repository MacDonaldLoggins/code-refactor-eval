FROM python:3.12-slim

ARG TARGETARCH

ENV HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
    ALL_PROXY="" all_proxy="" NO_PROXY="*" no_proxy="*" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libstdc++6 \
        ripgrep \
        xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Codex CLI requires Node.js. Pin the runtime so Agent/Judge behavior is reproducible.
RUN case "$TARGETARCH" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "unsupported Node architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://npmmirror.com/mirrors/node/v22.22.0/node-v22.22.0-linux-${node_arch}.tar.xz" \
        | tar -xJ --strip-components=1 -C /usr/local && \
    node --version && npm --version

COPY install_codex.sh /tmp/install_codex.sh
RUN bash /tmp/install_codex.sh && rm -f /tmp/install_codex.sh

# RewardKit version is part of the evaluation protocol and must be pinned.
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple \
        harbor-rewardkit==0.1.7

COPY repo/ /workspace/repo/

# CHANGE_ME: install the repository's locked runtime/dev dependencies here.
# Examples: pip install -r requirements.txt, uv sync --frozen, poetry install,
# or pipenv install --dev --deploy. Do not leave an unpinned dependency install.
RUN true

# Create both a git commit marker and a pristine filesystem baseline. The verifier
# may use either mechanism, but a case must not rely on a mutable external checkout.
RUN git init -q /workspace/repo && \
    git -C /workspace/repo add -A && \
    git -C /workspace/repo -c user.email=eval@harbor.local -c user.name=harbor \
        commit -qm "baseline: eval snapshot before agent" && \
    git -C /workspace/repo rev-parse HEAD > /workspace/repo/.base_commit && \
    cp -a /workspace/repo /workspace/baseline_repo && \
    rm -rf /workspace/baseline_repo/.git && \
    printf '%s\n' '.base_commit' >> /workspace/repo/.git/info/exclude && \
    useradd --create-home --uid 1000 evaluator && \
    chown -R evaluator:evaluator /workspace

USER evaluator

CMD ["sleep", "infinity"]

