#!/usr/bin/env bash
# Shared helpers for RediRecall's install/start/stop/restart scripts.
# Sourced by install.sh, start.sh, stop.sh, restart.sh — defines paths and
# helper functions only; performs no actions on its own.

# ── Repo root (directory that contains the entry scripts) ─────────────────────
# BASH_SOURCE[0] is this file (scripts/common.sh); the repo root is its parent's
# parent.
_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${_COMMON_DIR}/.." && pwd)"

# ── Platform-local data directory — MUST match redirecall/config.py ───────────
#   macOS:  ~/Library/Application Support/RediRecall
#   Linux:  $XDG_DATA_HOME/redirecall  or  ~/.local/share/redirecall
#   other:  ~/.redirecall
# Honors REDIRECALL_DATA_DIR (same env var the app reads).
data_dir() {
  if [ -n "${REDIRECALL_DATA_DIR:-}" ]; then
    printf '%s' "${REDIRECALL_DATA_DIR}"
    return
  fi
  case "$(uname -s)" in
    Darwin) printf '%s' "${HOME}/Library/Application Support/RediRecall" ;;
    Linux)  printf '%s' "${XDG_DATA_HOME:-${HOME}/.local/share}/redirecall" ;;
    *)      printf '%s' "${HOME}/.redirecall" ;;
  esac
}

DATA_DIR="$(data_dir)"

# ── Paths (all runtime state lives under DATA_DIR, never in the repo) ─────────
LOG_DIR="${DATA_DIR}/log"
APP_PID="${DATA_DIR}/redirecall.pid"
APP_LOG="${LOG_DIR}/redirecall.log"

REDIS_DIR="${DATA_DIR}/redis"
REDIS_CONF="${REDIS_DIR}/redis.conf"
REDIS_DATA="${REDIS_DIR}/data"
REDIS_PID="${REDIS_DIR}/redis.pid"
REDIS_LOG="${REDIS_DIR}/redis.log"

# ── Vendored Redis (repo-local, like .venv) ──────────────────────────────────
# install.sh puts a self-contained Redis 8 here and writes redis-env.sh with the
# resolved binary/module/launch-prefix; start.sh/stop.sh source it. On Linux the
# env may instead point at a reused system redis-server (see install.sh).
REDIS_HOME="${REPO_DIR}/.redis"
REDIS_VENDOR_LIB="${REDIS_HOME}/vendor-lib"
REDIS_ENV_FILE="${REDIS_HOME}/redis-env.sh"

# Source the resolved Redis environment (REDIS_SERVER / REDIS_CLI /
# REDIS_MODULE_SEARCH / REDIS_LAUNCH_PREFIX / REDIS_SOURCE). Returns non-zero if
# install.sh hasn't run yet.
load_redis_env() {
  [ -f "${REDIS_ENV_FILE}" ] || return 1
  # shellcheck disable=SC1090
  . "${REDIS_ENV_FILE}"
  return 0
}

# redis-cli wrapper: prefer the resolved REDIS_CLI, else a redis-cli on PATH.
rcli() {
  if [ -n "${REDIS_CLI:-}" ] && [ -x "${REDIS_CLI}" ]; then
    "${REDIS_CLI}" "$@"
  else
    redis-cli "$@"
  fi
}

# ── Ports ─────────────────────────────────────────────────────────────────────
# App (web UI): default 8420, overridable via REDIRECALL_PORT.
APP_PORT="${REDIRECALL_PORT:-8420}"
# App bind host: loopback by default (there is no auth yet — do not expose the
# port to a network without putting a reverse proxy / auth in front).
APP_HOST="${REDIRECALL_HOST:-127.0.0.1}"

# Dedicated Redis port for RediRecall's OWN instance. Default 6389 keeps clear
# of the standard 6379 so it never collides with a system/other Redis. The
# authoritative value is whatever install.sh wrote into redis.conf.
DEFAULT_REDIS_PORT=6389

# Read the Redis port that install.sh chose (from redis.conf), falling back to
# the app config, then the default.
redis_port() {
  if [ -f "${REDIS_CONF}" ]; then
    local p
    p="$(awk '/^[[:space:]]*port[[:space:]]+[0-9]+/ {print $2; exit}' "${REDIS_CONF}" 2>/dev/null)"
    if [ -n "${p}" ]; then printf '%s' "${p}"; return; fi
  fi
  if [ -f "${DATA_DIR}/config.json" ] && command -v python3 >/dev/null 2>&1; then
    local p
    p="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("redis",{}).get("port",""))' "${DATA_DIR}/config.json" 2>/dev/null)"
    if [ -n "${p}" ]; then printf '%s' "${p}"; return; fi
  fi
  printf '%s' "${DEFAULT_REDIS_PORT}"
}

# ── Small utilities ───────────────────────────────────────────────────────────
c_info()  { printf '\033[0;36m%s\033[0m\n' "$*"; }
c_ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
c_warn()  { printf '\033[0;33m%s\033[0m\n' "$*" >&2; }
c_err()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }

# Is a PID recorded in $1 alive?
pid_alive() {
  local f="$1" p
  [ -f "${f}" ] || return 1
  p="$(cat "${f}" 2>/dev/null || true)"
  [ -n "${p}" ] && kill -0 "${p}" 2>/dev/null
}

# Is anything listening on TCP port $1 (localhost)?
port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# Find a free TCP port at/after $1 (used by install.sh if the default is taken).
find_free_port() {
  local p="$1"
  while port_in_use "${p}"; do p=$((p + 1)); done
  printf '%s' "${p}"
}

# The venv python, if the venv exists; else system python3.
venv_python() {
  if [ -x "${REPO_DIR}/venv/bin/python" ]; then
    printf '%s' "${REPO_DIR}/venv/bin/python"
  elif [ -x "${REPO_DIR}/.venv/bin/python" ]; then
    printf '%s' "${REPO_DIR}/.venv/bin/python"
  else
    command -v python3 || command -v python
  fi
}
