#!/usr/bin/env bash
# Idempotent install of the python-tts CLI into $PYTHON_TTS_HOME.
# On success, prints the absolute path to the `tts` wrapper on stdout.
# Diagnostics go to stderr; exit non-zero on failure.
set -euo pipefail

REPO_URL="${PYTHON_TTS_REPO:-https://github.com/JLarky/python-tts.git}"
HOME_DIR="${PYTHON_TTS_HOME:-${HOME}/.local/share/python-tts}"
MIN_MAJOR=3
MIN_MINOR=10

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

need_cmd() {
  local cmd=$1 hint=$2
  command -v "$cmd" >/dev/null 2>&1 || die "${cmd} not found; ${hint}"
}

need_cmd git "install Git, then re-run."
need_cmd python3 "install Python ${MIN_MAJOR}.${MIN_MINOR}+, then re-run."

python3 -c "
import sys
req = (${MIN_MAJOR}, ${MIN_MINOR})
if sys.version_info[:2] < req:
    raise SystemExit(
        f'python3 is {sys.version_info.major}.{sys.version_info.minor}; '
        f'need {req[0]}.{req[1]}+'
    )
" || die "python3 is too old; install Python ${MIN_MAJOR}.${MIN_MINOR}+, then re-run."

if [[ ! -d "${HOME_DIR}/.git" ]]; then
  if [[ -e "${HOME_DIR}" ]] && [[ -n "$(ls -A "${HOME_DIR}" 2>/dev/null || true)" ]]; then
    die "${HOME_DIR} exists but is not a git checkout; move it aside or set PYTHON_TTS_HOME to another path."
  fi
  mkdir -p "$(dirname "${HOME_DIR}")"
  git clone --depth 1 "${REPO_URL}" "${HOME_DIR}" >&2 || die "git clone failed for ${REPO_URL}"
fi

cd "${HOME_DIR}"

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys' 2>/dev/null; then
  rm -rf .venv
  if ! python3 -m venv .venv; then
    die "python3 -m venv failed; on Debian/Ubuntu install python3-venv (and python3-pip), then re-run."
  fi
fi

STAMP=.venv/.requirements.stamp
if [[ ! -f "${STAMP}" || requirements.txt -nt "${STAMP}" ]]; then
  .venv/bin/pip install -q -U pip >&2 || die "pip self-upgrade failed; check network and re-run."
  .venv/bin/pip install -q -r requirements.txt >&2 || die "pip install failed; check network and re-run."
  touch "${STAMP}"
fi

.venv/bin/python -c 'import edge_tts' 2>/dev/null \
  || die "edge_tts import failed after install; try removing ${HOME_DIR}/.venv and re-running."

[[ -x "${HOME_DIR}/tts" ]] || die "tts wrapper missing at ${HOME_DIR}/tts"

printf '%s\n' "${HOME_DIR}/tts"
