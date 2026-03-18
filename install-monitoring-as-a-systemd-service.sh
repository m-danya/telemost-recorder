#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="telemost-recorder-monitoring.service"
CALLER_DIR="$(pwd -P)"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LOCAL_BIN_DIR="$HOME/.local/bin"
UNIT_PATH="$SYSTEMD_USER_DIR/$SERVICE_NAME"
LAUNCHER_PATH="$LOCAL_BIN_DIR/telemost-recorder-monitoring-service"
TRIGGER_PATH="$LOCAL_BIN_DIR/telemost-recorder-trigger"

require_command() {
    local command_name="$1"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "missing required command: $command_name" >&2
        exit 1
    fi
}

write_launcher() {
    local uv_bin="$1"

    {
        printf '%s\n' '#!/usr/bin/env bash'
        printf '%s\n' 'set -euo pipefail'
        printf 'export PATH=%q\n' "$PATH"
        printf 'cd -- %q\n' "$CALLER_DIR"
        printf 'exec %q run --project %q telemost-recorder run\n' "$uv_bin" "$PROJECT_DIR"
    } > "$LAUNCHER_PATH"

    chmod 0755 "$LAUNCHER_PATH"
}

write_trigger_launcher() {
    local uv_bin="$1"

    {
        printf '%s\n' '#!/usr/bin/env bash'
        printf '%s\n' 'set -euo pipefail'
        printf 'export PATH=%q\n' "$PATH"
        printf 'exec %q run --project %q telemost-recorder trigger\n' "$uv_bin" "$PROJECT_DIR"
    } > "$TRIGGER_PATH"

    chmod 0755 "$TRIGGER_PATH"
}

write_unit() {
    {
        printf '%s\n' '[Unit]'
        printf '%s\n' 'Description=Telemost Recorder monitoring service'
        printf '%s\n' 'After=default.target'
        printf '\n%s\n' '[Service]'
        printf '%s\n' 'Type=simple'
        printf '%s\n' 'Restart=always'
        printf '%s\n' 'RestartSec=10'
        printf '%s\n' 'SyslogIdentifier=telemost-recorder-monitoring'
        printf 'ExecStart=%s\n' "$LAUNCHER_PATH"
        printf '\n%s\n' '[Install]'
        printf '%s\n' 'WantedBy=default.target'
    } > "$UNIT_PATH"
}

require_command systemctl
require_command uv

mkdir -p "$SYSTEMD_USER_DIR" "$LOCAL_BIN_DIR"

UV_BIN="$(command -v uv)"
write_launcher "$UV_BIN"
write_trigger_launcher "$UV_BIN"
write_unit

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME" >/dev/null

if systemctl --user is-active --quiet "$SERVICE_NAME"; then
    systemctl --user restart "$SERVICE_NAME"
else
    systemctl --user start "$SERVICE_NAME"
fi

cat <<EOF
Installed $SERVICE_NAME
Project directory: $PROJECT_DIR
Service working directory: $CALLER_DIR
Unit file: $UNIT_PATH
Launcher: $LAUNCHER_PATH
Trigger helper: $TRIGGER_PATH

The service reads .env and relative paths like recordings/ from:
  $CALLER_DIR

Useful commands:
  systemctl --user status $SERVICE_NAME
  journalctl --user -u $SERVICE_NAME -f
  telemost-recorder-trigger
EOF
