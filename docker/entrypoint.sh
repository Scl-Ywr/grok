#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
APP_DIR="${APP_DIR:-/app}"
CONFIG_FILE="${CONFIG_FILE:-$DATA_DIR/config.json}"
CPA_AUTH_DIR="${CPA_AUTH_DIR:-$DATA_DIR/auth}"
ACCOUNTS_DIR="${ACCOUNTS_DIR:-$DATA_DIR/accounts}"
WEBUI_HOST="${WEBUI_HOST:-0.0.0.0}"
WEBUI_PORT="${WEBUI_PORT:-8787}"

mkdir -p "$DATA_DIR" "$CPA_AUTH_DIR" "$ACCOUNTS_DIR"

# First boot: seed config from example / docker template
if [[ ! -f "$CONFIG_FILE" ]]; then
  if [[ -f "$APP_DIR/config.docker.json" ]]; then
    cp "$APP_DIR/config.docker.json" "$CONFIG_FILE"
  elif [[ -f "$APP_DIR/config.example.json" ]]; then
    cp "$APP_DIR/config.example.json" "$CONFIG_FILE"
  else
    echo '{}' > "$CONFIG_FILE"
  fi
  echo "[entrypoint] seeded $CONFIG_FILE"
fi

# Soft-patch config: ensure docker-friendly defaults without wiping user values
python3 - <<'PY'
import json, os
from pathlib import Path

cfg_path = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))
auth_dir = os.environ.get("CPA_AUTH_DIR", "/data/auth")
try:
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
except Exception:
    data = {}

changed = False
defaults = {
    "cpa_auth_dir": auth_dir,
    "register_count": data.get("register_count") or 1,
    "enable_nsfw": True if "enable_nsfw" not in data else data.get("enable_nsfw"),
    "email_provider": data.get("email_provider") or "yyds",
}
for k, v in defaults.items():
    if k not in data or data.get(k) in ("", None):
        data[k] = v
        changed = True
# Always prefer container auth dir if empty / host path that won't exist
if not str(data.get("cpa_auth_dir") or "").strip():
    data["cpa_auth_dir"] = auth_dir
    changed = True
if str(data.get("cpa_auth_dir") or "").startswith("/Users/"):
    data["cpa_auth_dir"] = auth_dir
    changed = True
if changed:
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[entrypoint] patched config defaults -> {cfg_path}")
else:
    print(f"[entrypoint] config ready: {cfg_path}")
PY

# Point app working files into /data (accounts_*.txt live next to webui.py by default)
# Symlink common outputs into /data for persistence.
cd "$APP_DIR"
if [[ ! -e "$APP_DIR/config.json" ]]; then
  ln -sfn "$CONFIG_FILE" "$APP_DIR/config.json"
fi

# Soft-link account files directory: write accounts into /data/accounts via wrapper env
export PYTHONPATH="$APP_DIR:${PYTHONPATH:-}"
export RUNNING_IN_DOCKER=1
export CHROME_PATH="${CHROME_PATH:-/usr/bin/chromium}"
export BROWSER_HEADLESS="${BROWSER_HEADLESS:-1}"
export CONFIG_FILE="$CONFIG_FILE"
export ACCOUNTS_DIR="$ACCOUNTS_DIR"
export CPA_AUTH_DIR="$CPA_AUTH_DIR"
export DATA_DIR="$DATA_DIR"

cmd="${1:-webui}"
shift || true

case "$cmd" in
  webui)
    echo "[entrypoint] starting WebUI on ${WEBUI_HOST}:${WEBUI_PORT}"
    # Use Flask built-in for simplicity (browser automation is the bottleneck, not HTTP)
    exec python3 -u "$APP_DIR/webui.py" --host "$WEBUI_HOST" --port "$WEBUI_PORT"
    ;;
  cli)
    echo "[entrypoint] starting CLI register"
    exec python3 -u "$APP_DIR/grok_register_ttk.py" cli "$@"
    ;;
  upload)
    echo "[entrypoint] upload_to_cpa"
    exec python3 -u "$APP_DIR/upload_to_cpa.py" "$@"
    ;;
  bash|sh)
    exec /bin/bash "$@"
    ;;
  *)
    echo "[entrypoint] exec: $cmd $*"
    exec "$cmd" "$@"
    ;;
esac
