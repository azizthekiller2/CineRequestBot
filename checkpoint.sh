#!/usr/bin/env bash
# ==============================================================================
# CineRequestBot - Code Checkpoint & Revert System
# ==============================================================================
# Usage:
#   ./checkpoint.sh save [label]     - Snapshot current code state to .bak/
#   ./checkpoint.sh restore [id]     - Revert code state to checkpoint (default: latest)
#   ./checkpoint.sh list             - List all stored checkpoints
#   ./checkpoint.sh diff             - Compare current code with latest checkpoint
#   ./restore.sh                     - Single-command shortcut to restore latest
# ==============================================================================

set -e

# Find repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SCRIPT_DIR/telegram-bot" ]; then
    ROOT_DIR="$SCRIPT_DIR"
elif [ -d "$SCRIPT_DIR/../telegram-bot" ]; then
    ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
    ROOT_DIR="$SCRIPT_DIR"
fi

BAK_DIR="$ROOT_DIR/.bak"
LATEST_LINK="$BAK_DIR/latest"
HISTORY_FILE="$BAK_DIR/history.log"

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${CYAN}[CHECKPOINT]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[CHECKPOINT]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_err() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ------------------------------------------------------------------------------
# Action: SAVE CHECKPOINT
# ------------------------------------------------------------------------------
action_save() {
    local label="$1"
    local timestamp
    timestamp="$(date +'%Y%m%d_%H%M%S')"
    
    local cp_name="checkpoint_${timestamp}"
    if [ -n "$label" ]; then
        # sanitize label (allow letters, numbers, dashes, underscores)
        local safe_label
        safe_label=$(echo -n "$label" | tr -cs 'a-zA-Z0-9_-' '_' | sed 's/^_*//;s/_*$//')
        if [ -n "$safe_label" ]; then
            cp_name="checkpoint_${timestamp}_${safe_label}"
        fi
    fi

    local target_dir="$BAK_DIR/$cp_name"
    mkdir -p "$target_dir"

    log_info "Creating code checkpoint: ${cp_name}..."

    # Git details if available
    local git_info="No git repo"
    if [ -d "$ROOT_DIR/.git" ]; then
        local git_branch git_hash git_msg
        git_branch="$(cd "$ROOT_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
        git_hash="$(cd "$ROOT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
        git_msg="$(cd "$ROOT_DIR" && git log -1 --pretty=%s 2>/dev/null || echo '')"
        git_info="git: [${git_branch}] ${git_hash} - ${git_msg}"
    fi

    # Copy code from telegram-bot excluding dynamic/heavy files
    if [ -d "$ROOT_DIR/telegram-bot" ]; then
        mkdir -p "$target_dir/telegram-bot"
        
        # Use rsync if available for cleaner exclusions, otherwise tar/find
        if command -v rsync >/dev/null 2>&1; then
            rsync -avq \
                --exclude='venv/' \
                --exclude='.venv/' \
                --exclude='__pycache__/' \
                --exclude='*.pyc' \
                --exclude='sessions/' \
                --exclude='*.session' \
                --exclude='*.session-journal' \
                --exclude='.env' \
                "$ROOT_DIR/telegram-bot/" "$target_dir/telegram-bot/"
        else
            tar -C "$ROOT_DIR" \
                --exclude='telegram-bot/venv' \
                --exclude='telegram-bot/.venv' \
                --exclude='telegram-bot/__pycache__' \
                --exclude='telegram-bot/*.pyc' \
                --exclude='telegram-bot/sessions' \
                --exclude='telegram-bot/*.session*' \
                --exclude='telegram-bot/.env' \
                -cf - telegram-bot | tar -C "$target_dir" -xf -
        fi
    fi

    # Backup root deployment files if present
    for f in Dockerfile railway.json README.md requirements.txt; do
        if [ -f "$ROOT_DIR/$f" ]; then
            cp "$ROOT_DIR/$f" "$target_dir/" 2>/dev/null || true
        fi
    done

    # Write metadata
    cat <<EOF > "$target_dir/meta.txt"
ID: $cp_name
Timestamp: $(date '+%Y-%m-%d %H:%M:%S %Z')
Label: ${label:-"(auto)"}
Commit: $git_info
Created By: $(whoami)@$(hostname)
EOF

    # Update latest symlink/copy
    rm -rf "$LATEST_LINK"
    ln -sfn "$cp_name" "$LATEST_LINK" 2>/dev/null || cp -r "$target_dir" "$LATEST_LINK"

    # Append to history.log
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${cp_name} | Label: ${label:-"(auto)"} | ${git_info}" >> "$HISTORY_FILE"

    log_success "Checkpoint created successfully!"
    echo "  Location: $target_dir"
    echo "  Latest:   $LATEST_LINK"
    echo "  Revert anytime using: ./restore.sh (or ./checkpoint.sh restore)"
}

# ------------------------------------------------------------------------------
# Action: RESTORE CHECKPOINT
# ------------------------------------------------------------------------------
action_restore() {
    local target="$1"
    local source_dir=""

    if [ -z "$target" ] || [ "$target" = "latest" ]; then
        if [ ! -d "$LATEST_LINK" ] && [ ! -L "$LATEST_LINK" ]; then
            log_err "No 'latest' checkpoint found in $BAK_DIR."
            exit 1
        fi
        source_dir="$LATEST_LINK"
        log_info "Restoring from latest checkpoint..."
    else
        if [ -d "$BAK_DIR/$target" ]; then
            source_dir="$BAK_DIR/$target"
            log_info "Restoring from checkpoint: $target..."
        else
            log_err "Checkpoint '$target' does not exist in $BAK_DIR."
            echo "Available checkpoints:"
            action_list
            exit 1
        fi
    fi

    if [ -f "$source_dir/meta.txt" ]; then
        echo -e "${YELLOW}Checkpoint Info:${NC}"
        cat "$source_dir/meta.txt" | sed 's/^/  /'
    fi

    # Restore telegram-bot files (preserving existing active .env, sessions, venv)
    if [ -d "$source_dir/telegram-bot" ]; then
        log_info "Copying code back to $ROOT_DIR/telegram-bot..."
        if command -v rsync >/dev/null 2>&1; then
            rsync -avq \
                --exclude='venv/' \
                --exclude='.venv/' \
                --exclude='sessions/' \
                --exclude='*.session*' \
                --exclude='.env' \
                "$source_dir/telegram-bot/" "$ROOT_DIR/telegram-bot/"
        else
            cp -r "$source_dir/telegram-bot/"* "$ROOT_DIR/telegram-bot/"
        fi
    fi

    # Restore root files
    for f in Dockerfile railway.json README.md requirements.txt; do
        if [ -f "$source_dir/$f" ]; then
            cp "$source_dir/$f" "$ROOT_DIR/$f" 2>/dev/null || true
        fi
    done

    log_success "Code files reverted successfully from checkpoint!"

    # If running with systemd (e.g. cinerequestbot), offer/trigger restart
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet cinerequestbot 2>/dev/null || systemctl list-unit-files 2>/dev/null | grep -q "^cinerequestbot.service"; then
            log_info "Detected cinerequestbot.service! Restarting bot service..."
            if [ "$EUID" -ne 0 ]; then
                sudo systemctl restart cinerequestbot || log_warn "Could not restart service automatically. Please run: sudo systemctl restart cinerequestbot"
            else
                systemctl restart cinerequestbot || log_warn "Could not restart service."
            fi
            sleep 2
            if systemctl is-active --quiet cinerequestbot 2>/dev/null; then
                log_success "cinerequestbot service is active and running!"
            else
                log_warn "cinerequestbot service status:"
                systemctl status cinerequestbot --no-pager -n 5 || true
            fi
        fi
    fi

    echo "Revert complete."
}

# ------------------------------------------------------------------------------
# Action: LIST CHECKPOINTS
# ------------------------------------------------------------------------------
action_list() {
    if [ ! -d "$BAK_DIR" ]; then
        log_warn "No checkpoints found. ($BAK_DIR does not exist)"
        return 0
    fi

    echo -e "${CYAN}Available Checkpoints:${NC}"
    echo "------------------------------------------------------------"
    local count=0
    for cp in "$BAK_DIR"/checkpoint_*; do
        if [ -d "$cp" ]; then
            count=$((count + 1))
            local name
            name="$(basename "$cp")"
            local is_latest=""
            if [ -L "$LATEST_LINK" ] && [ "$(readlink -f "$LATEST_LINK")" = "$(readlink -f "$cp")" ]; then
                is_latest=" ${GREEN}(latest)${NC}"
            fi
            local info=""
            if [ -f "$cp/meta.txt" ]; then
                local ts label
                ts=$(grep "^Timestamp:" "$cp/meta.txt" | cut -d' ' -f2-)
                label=$(grep "^Label:" "$cp/meta.txt" | cut -d' ' -f2-)
                info="[$ts] Label: $label"
            fi
            echo -e "  • ${YELLOW}${name}${NC}${is_latest} - ${info}"
        fi
    done

    if [ $count -eq 0 ]; then
        echo "  (No checkpoints created yet)"
    fi
    echo "------------------------------------------------------------"
}

# ------------------------------------------------------------------------------
# Action: DIFF
# ------------------------------------------------------------------------------
action_diff() {
    if [ ! -d "$LATEST_LINK" ]; then
        log_err "No latest checkpoint to compare with."
        exit 1
    fi
    log_info "Comparing current code with latest checkpoint..."
    diff -ur \
        -x 'venv' -x '.venv' -x '__pycache__' -x '*.pyc' -x 'sessions' -x '*.session*' -x '.env' -x '.git' \
        "$LATEST_LINK/telegram-bot" "$ROOT_DIR/telegram-bot" || true
}

# ------------------------------------------------------------------------------
# Entrypoint Dispatcher
# ------------------------------------------------------------------------------
case "${1:-save}" in
    save|create|backup)
        action_save "$2"
        ;;
    restore|revert)
        action_restore "$2"
        ;;
    list|ls)
        action_list
        ;;
    diff)
        action_diff
        ;;
    help|--help|-h)
        echo "Usage:"
        echo "  ./checkpoint.sh save [label]     - Save snapshot to .bak/"
        echo "  ./checkpoint.sh restore [id]     - Revert to checkpoint (defaults to latest)"
        echo "  ./checkpoint.sh list             - List saved checkpoints"
        echo "  ./checkpoint.sh diff             - Diff current code against latest checkpoint"
        echo "  ./restore.sh                     - Single command revert to latest checkpoint"
        ;;
    *)
        # Default behavior if argument looks like a label
        action_save "$1"
        ;;
esac
