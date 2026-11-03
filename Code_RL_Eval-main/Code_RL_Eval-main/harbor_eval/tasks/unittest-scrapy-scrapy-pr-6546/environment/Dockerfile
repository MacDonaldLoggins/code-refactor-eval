FROM node:20-bookworm-slim AS node-runtime

FROM python:3.11-slim-bookworm

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 install -y --no-install-recommends curl git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 agent

RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-maxtimeout 120000

COPY install_claude_code.sh /tmp/install_claude_code.sh
RUN SKIP_APT_INSTALL=1 CLAUDE_INSTALL_METHOD=npm bash /tmp/install_claude_code.sh \
    && rm -f /tmp/install_claude_code.sh

COPY install_codex.sh /tmp/install_codex.sh
COPY codex_config.toml /tmp/codex_config.toml
RUN SKIP_APT_INSTALL=1 CODEX_VERSION=0.146.0 CODEX_CONFIG_SRC=/tmp/codex_config.toml \
        bash /tmp/install_codex.sh \
    && rm -f /tmp/install_codex.sh /tmp/codex_config.toml

COPY requirements.lock /tmp/unittest-rl-current.lock
RUN --mount=type=cache,target=/root/.cache/pip python -m venv /opt/unittest-rl/runtimes/current \
    && /opt/unittest-rl/runtimes/current/bin/python -m pip install --retries 10 --timeout 120 -r /tmp/unittest-rl-current.lock \
    && ln -s /opt/unittest-rl/runtimes/current /opt/unittest-rl/runtime

# Harbor executes agent commands through a login shell. Debian's /etc/profile
# rebuilds PATH for login shells, so restore the frozen runtime after that
# reset instead of relying only on Docker's inherited ENV PATH.
RUN printf '%s\n' \
        'VIRTUAL_ENV=/opt/unittest-rl/runtimes/current' \
        'export VIRTUAL_ENV' \
        'PATH="$VIRTUAL_ENV/bin:$PATH"' \
        'export PATH' \
        > /etc/profile.d/unittest-rl-runtime.sh \
    && chmod 0644 /etc/profile.d/unittest-rl-runtime.sh

WORKDIR /app
COPY repo/ /app/repository/
RUN mkdir -p /app/candidate_tests /app/.candidate_tmp \
    && chown -R agent:agent /app/candidate_tests /app/.candidate_tmp \
    && chown -R root:root /app/repository \
    && chmod -R a-w /app/repository \
    && install -d -o agent -g agent /home/agent/.codex \
    && cp /root/.codex/config.toml /home/agent/.codex/config.toml \
    && chown agent:agent /home/agent/.codex/config.toml

ENV VIRTUAL_ENV=/opt/unittest-rl/runtimes/current
ENV PATH=$VIRTUAL_ENV/bin:$PATH
ENV PYTHONPATH=/app/repository
ENV PYTHONNOUSERSITE=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
ENV TMPDIR=/app/.candidate_tmp

USER agent
