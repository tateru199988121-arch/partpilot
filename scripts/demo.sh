#!/usr/bin/env bash
# 可重複執行的離線示範；不下載套件、不呼叫外部 API。
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PARTPILOT_PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]] && command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "找不到 Python；請先建立 .venv 並安裝 requirements.txt。" >&2
  exit 1
fi
serve_mode=demo
if [[ "${1:-}" == "--configured-mode" ]]; then
  serve_mode="${LLM_MODE:-demo}"
fi
export LLM_MODE=demo
if [[ -z "${PARTPILOT_INIT_PASSWORD:-}" ]]; then
  if [[ ! -f "${PARTPILOT_STATE_DIR:-data/state}/users.json" ]]; then
    export PARTPILOT_INIT_PASSWORD="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(18))')"
    echo "本次示範初始密碼：$PARTPILOT_INIT_PASSWORD"
    echo "請保存此密碼；重新啟動時設定 PARTPILOT_PASSWORD，既有帳號密碼不會重設。"
  fi
fi
export PARTPILOT_PASSWORD="${PARTPILOT_PASSWORD:-${PARTPILOT_INIT_PASSWORD:-}}"
export PARTPILOT_RAW_DIR=data/sample
"$PYTHON" scripts/make_sample_data.py
"$PYTHON" - <<'PY'
from pathlib import Path
from partpilot.auth import init_users, UserStore
from partpilot.config import load_config

cfg = load_config()
path = Path(cfg.state_dir) / "users.json"
if UserStore(str(path))._read():
    print("帳號已存在，保留既有帳號與密碼。")
else:
    print(f"已建立 {init_users(str(path), cfg.init_password)} 個示範帳號。")
PY
"$PYTHON" -m partpilot clean --user sys_admin
export LLM_MODE="$serve_mode"
exec "$PYTHON" -u -m partpilot serve --host "${PARTPILOT_HOST:-127.0.0.1}" --port 18765
