#!/usr/bin/env bash
# ==============================================================================
# Single-Command Revert Tool
# Reverts the code back to the latest .bak checkpoint and restarts the bot service
# ==============================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/checkpoint.sh" restore "${1:-latest}"
