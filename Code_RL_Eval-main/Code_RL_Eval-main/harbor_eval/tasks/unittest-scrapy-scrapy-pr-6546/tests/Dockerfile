FROM node:20-bookworm-slim AS node-runtime

FROM python:3.11-slim-bookworm

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm install -g --no-audit --no-fund @openai/codex@0.146.0 @anthropic-ai/claude-code@2.1.167

COPY requirements.lock /tmp/unittest-rl-current.lock
RUN --mount=type=cache,target=/root/.cache/pip python -m venv /opt/unittest-rl/runtimes/current \
    && /opt/unittest-rl/runtimes/current/bin/python -m pip install --retries 10 --timeout 120 -r /tmp/unittest-rl-current.lock \
    && ln -s /opt/unittest-rl/runtimes/current /opt/unittest-rl/runtime

WORKDIR /app
COPY fixtures/legacy_repository/ /app/repository/
RUN chmod -R a-w /app/repository

COPY prepared_verifier.py /tests/prepared_verifier.py
COPY reward/e2_observer.py /tests/e2_observer.py
COPY execution_reporter.py /tests/execution_reporter.py
COPY fixtures/evaluation_assets/ /tests/evaluation_assets/
COPY hard_gates/task_binding.json /tests/task_binding.json
COPY execution-profiles.json /tests/execution-profiles.json
COPY execution-profile-bindings.json /tests/execution-profile-bindings.json
COPY hard_gates/trace_bridge.py /tests/trace_bridge.py
COPY hard_gates/leakage.py /tests/leakage.py
COPY reward/e2.py /tests/e2.py
COPY rubric/rubric.py /tests/judge.py
COPY rubric/judge_runtime.py /tests/judge_runtime.py
COPY reward/reward.py /tests/reward.py
COPY reward/e2-evidence-contract.json /tests/e2-evidence-contract.json
COPY rubric/judge-config.json /tests/judge-config.json
COPY rubric/judge-replay.json /tests/judge-replay.json
COPY hard_gates/public-instruction.md /tests/public-instruction.md
COPY verifier.py /tests/verifier.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh

ENV PATH=/opt/unittest-rl/runtimes/current/bin:$PATH
ENV PYTHONPATH=/app/repository
ENV PYTHONNOUSERSITE=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
