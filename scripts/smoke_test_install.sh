#!/usr/bin/env bash
# Smoke test: install gioe-libs into an isolated venv and verify key imports.
# Catches import regressions that only appear after pip install (not PYTHONPATH=.).
#
# Usage:
#   ./scripts/smoke_test_install.sh
#
# Exit codes:
#   0  all imports succeeded
#   1  one or more imports failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$(mktemp -d)/smoke_venv"

cleanup() {
    rm -rf "$VENV_DIR"
}
trap cleanup EXIT

echo "==> Creating isolated venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

echo "==> Installing gioe-libs from $REPO_ROOT"
"$PIP" install --quiet "$REPO_ROOT"

echo "==> Verifying imports"
"$PYTHON" - <<'EOF'
from gioe_libs.alerting import ResourceMonitor, AlertManager
from gioe_libs.structured_logging import setup_logging
from gioe_libs.cron_runner import CronJob
print("OK: all imports succeeded")
EOF

echo "==> Smoke test passed"
