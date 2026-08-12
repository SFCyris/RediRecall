#!/usr/bin/env bash
# RediRecall — start the dedicated local Redis, then the app.
#
# Idempotent: if either is already running it is left alone. The app binds to
# loopback (127.0.0.1) by default — there is no built-in auth, so do NOT
# expose the port to a network without a reverse proxy / auth in front. Override
# with REDIRECALL_HOST / REDIRECALL_PORT, or a bare port as the first argument.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

[ -n "${1:-}" ] && APP_PORT="$1"   # optional: ./start.sh 9000
RPORT="$(redis_port)"

mkdir -p "${LOG_DIR}" "${REDIS_DIR}" "${REDIS_DATA}"

# ── Preconditions ─────────────────────────────────────────────────────────────
if ! load_redis_env; then
  c_err "Redis not provisioned (no ${REDIS_ENV_FILE}) — run ./install.sh first."
  exit 1
fi
if [ ! -f "${REDIS_CONF}" ]; then
  c_err "No Redis config at ${REDIS_CONF} — run ./install.sh first."
  exit 1
fi
if [ ! -x "${REDIS_SERVER}" ] && ! command -v "${REDIS_SERVER}" >/dev/null 2>&1; then
  c_err "Resolved redis-server '${REDIS_SERVER}' is missing — re-run ./install.sh."
  exit 1
fi
if [ ! -x "$(venv_python)" ] && [ ! -x "${REPO_DIR}/venv/bin/python" ]; then
  c_err "No virtualenv — run ./install.sh first."
  exit 1
fi
PY="${REPO_DIR}/venv/bin/python"
[ -x "${PY}" ] || PY="$(venv_python)"

# ── 1. Redis ──────────────────────────────────────────────────────────────────
# DYLD_FALLBACK_LIBRARY_PATH points at the vendored libunwind shim on macOS; it
# is empty (and ignored) on Linux.
if rcli -p "${RPORT}" ping >/dev/null 2>&1; then
  c_info "Redis already responding on 127.0.0.1:${RPORT}."
else
  c_info "Starting dedicated Redis on 127.0.0.1:${RPORT} (${REDIS_SOURCE:-local})…"
  # Launch DIRECTLY (not via nohup): macOS SIP strips DYLD_* when exec'ing the
  # protected /usr/bin/nohup, which would break the vendored libunwind shim.
  # Redis self-daemonizes (daemonize yes) and writes its own pidfile; the log
  # goes to the logfile set in the conf.
  DYLD_FALLBACK_LIBRARY_PATH="${REDIS_DYLD_FALLBACK:-}" "${REDIS_SERVER}" "${REDIS_CONF}" \
    || { c_err "redis-server failed to launch. Last log lines:"; tail -n 20 "${REDIS_LOG}" >&2 || true; exit 1; }
  # Wait for it to accept connections (a module-load failure aborts the daemon,
  # so the ping never succeeds and we surface the log below).
  up=0
  for _ in $(seq 1 30); do
    if rcli -p "${RPORT}" ping >/dev/null 2>&1; then up=1; break; fi
    sleep 0.5
  done
  if [ "${up}" != "1" ]; then
    c_err "Redis did not come up (search module may have failed to load). Last log lines:"
    tail -n 20 "${REDIS_LOG}" >&2 || true
    exit 1
  fi
  c_ok "Redis up (pid $(cat "${REDIS_PID}" 2>/dev/null || echo '?'))."
fi

# Confirm the Query Engine (search) is loaded — the app needs FT.* / vectors.
if rcli -p "${RPORT}" FT._LIST >/dev/null 2>&1; then
  c_ok "Query Engine (search) available."
else
  c_warn "Redis is up but the search module did not load — FT.* will fail."
  c_warn "Re-run ./install.sh (it locates and loads redisearch.so)."
fi

# ── 2. App ────────────────────────────────────────────────────────────────────
if pid_alive "${APP_PID}"; then
  c_info "App already running (pid $(cat "${APP_PID}")) — leaving it."
elif port_in_use "${APP_PORT}"; then
  # Identify the squatter before shrugging: an instance started by hand (no
  # pidfile) is still OURS, and pretending "another process" has the port while
  # the banner below announces the repo version made a failed swap look like a
  # successful one (a stale 1.5.0 kept serving under a "v1.7.0 is running" banner).
  _live="$(curl -fsS -m 2 "http://127.0.0.1:${APP_PORT}/api/health" 2>/dev/null || true)"
  if printf '%s' "${_live}" | grep -q '"app" *: *"RediRecall"'; then
    _lv="$(printf '%s' "${_live}" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
    c_warn "Port ${APP_PORT} already serves RediRecall v${_lv:-?} — an instance these scripts don't track (no pidfile)."
    c_warn "Not starting a second app. Run ./restart.sh (stop.sh now finds and stops port-squatting instances too)."
  else
    c_err "Port ${APP_PORT} is in use by something that is NOT RediRecall — not starting."
    exit 1
  fi
else
  c_info "Starting RediRecall on http://${APP_HOST}:${APP_PORT}…"
  # --app-dir puts the repo on sys.path so `main:app` imports without a cd — and
  # without a wrapping subshell, so $! is the uvicorn process itself. (The subshell
  # form captured the wrapper's PID, so stop.sh killed the wrapper and orphaned the
  # real server.) After the package split this becomes the `redirecall` console script.
  REDIRECALL_PORT="${APP_PORT}" nohup "${PY}" -m uvicorn redirecall.main:app \
    --app-dir "${REPO_DIR}" --host "${APP_HOST}" --port "${APP_PORT}" >>"${APP_LOG}" 2>&1 &
  echo $! > "${APP_PID}"
  # Poll /api/health until ready.
  up=0
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/api/health" >/dev/null 2>&1; then up=1; break; fi
    if ! pid_alive "${APP_PID}"; then
      c_err "App process exited during startup. Last log lines:"
      tail -n 25 "${APP_LOG}" >&2 || true
      exit 1
    fi
    sleep 1
  done
  if [ "${up}" != "1" ]; then
    c_err "App did not become healthy in time. Last log lines:"
    tail -n 25 "${APP_LOG}" >&2 || true
    exit 1
  fi
  c_ok "App up (pid $(cat "${APP_PID}"))."
fi

# ── Status banner ─────────────────────────────────────────────────────────────
# Version: ask the RUNNING server what it is — the repo file only says what the
# next start would serve, and quoting it after a skipped start announced a
# version that was not actually running. File value is the fallback only.
APP_VERSION="$(curl -fsS -m 2 "http://127.0.0.1:${APP_PORT}/api/health" 2>/dev/null \
  | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
[ -z "${APP_VERSION}" ] && APP_VERSION="$(sed -n 's/^__version__ *= *["'"'"']\(.*\)["'"'"']/\1/p' "${REPO_DIR}/redirecall/__init__.py" 2>/dev/null)"
[ -z "${APP_VERSION}" ] && APP_VERSION="?"

# Best-effort primary LAN IPv4, for the case where the app is bound to all interfaces.
_lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    for _if in en0 en1 en2 en3; do ip="$(ipconfig getifaddr "${_if}" 2>/dev/null || true)"; [ -n "${ip}" ] && break; done
  fi
  [ -z "${ip}" ] && command -v hostname >/dev/null 2>&1 && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "${ip}"
}

# Every URL the UI can be reached at, given the bind host.
APP_URLS=()
case "${APP_HOST}" in
  0.0.0.0|::|"")
    APP_URLS+=("http://localhost:${APP_PORT}")
    _ip="$(_lan_ip)"; [ -n "${_ip}" ] && APP_URLS+=("http://${_ip}:${APP_PORT}  (LAN)")
    ;;
  127.0.0.1|localhost)
    APP_URLS+=("http://127.0.0.1:${APP_PORT}" "http://localhost:${APP_PORT}")
    ;;
  *)
    APP_URLS+=("http://${APP_HOST}:${APP_PORT}")
    ;;
esac

c_info ""
c_ok "══ RediRecall v${APP_VERSION} is running ══"
c_info "  Web UI:   ${APP_URLS[0]}"
for _u in "${APP_URLS[@]:1}"; do c_info "            ${_u}"; done
c_info "  Redis:    127.0.0.1:${RPORT} (dedicated, AOF everysec)"
c_info "  App log:  ${APP_LOG}"
c_info "  Redis log:${REDIS_LOG}"
c_info "  Stop:     ${REPO_DIR}/stop.sh"
if [ "${APP_HOST}" != "127.0.0.1" ] && [ "${APP_HOST}" != "localhost" ]; then
  c_warn "  NOTE: bound to ${APP_HOST} — the API has no built-in auth. Put a reverse proxy with auth in front before exposing it."
fi
