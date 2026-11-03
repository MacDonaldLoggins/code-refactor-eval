#!/bin/bash
set -euo pipefail

exec python3 /tests/verifier.py \
  --repo /workspace/repo \
  --tests /tests \
  --logs /logs/verifier

