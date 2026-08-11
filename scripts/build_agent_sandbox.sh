#!/usr/bin/env bash
#
# build_agent_sandbox.sh — hard-isolation executor for safety_auto_research
# agent-mode research tasks.
#
# Runs an arbitrary research command INSIDE a disposable Linux container
# (Colima/Docker) with:
#   * dataset mounted READ-ONLY              (AGENT_DATA_DIR   -> /data)
#   * code/repo mounted READ-ONLY            (AGENT_REPO_DIR   -> /repo)
#   * scratch mounted READ-WRITE, ephemeral  (AGENT_SCRATCH_DIR -> /scratch)
#   * network OFF by default                 (--network none)
#   * no-new-privileges + ALL caps dropped   (defence against container escape)
#   * memory / cpu / pids limits
#
# This is the F3 "hard isolation" path. Because the agent (codex/claude) runs
# on the HOST and calls run_capability, the execution step (running the research
# artifact) is what we confine — not the agent process itself.
#
# Fallback: if Docker is unavailable, OR AGENT_SANDBOX_DISABLE=1, the command
# runs directly on the host with the same env vars set (SOFT isolation — a
# convenience boundary only, NOT a security boundary). The caller is warned.
#
# R9 fix (fail-closed + unforgeable isolation proof):
#   * AGENT_SANDBOX_REQUIRE_HARD=1 turns the soft fallback into a hard error
#     (exit 78 / EX_CONFIG) instead of silently running unconfined code on the
#     host. Use this for any run whose result feeds a safety claim.
#   * The launcher — which runs on the HOST and is NOT under the control of the
#     sandboxed payload — writes "$AGENT_SCRATCH_DIR/.sandbox_mode" recording the
#     isolation path actually taken. Callers MUST read that file instead of
#     trusting fields the measured program writes into its own result.json
#     (a payload can trivially claim {"network_blocked": true}).
#
set -euo pipefail

# ---- configuration (env-overridable) ----------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_IMAGE="${AGENT_SANDBOX_IMAGE:-safety-research-sandbox:latest}"
AGENT_REPO_DIR="${AGENT_REPO_DIR:-$REPO_ROOT}"
AGENT_DATA_DIR="${AGENT_DATA_DIR:-$REPO_ROOT/data/kaggle}"
AGENT_SCRATCH_DIR="${AGENT_SCRATCH_DIR:-${HOME:-/tmp}/.cache/agent_sandbox_scratch}"
AGENT_SANDBOX_NETWORK="${AGENT_SANDBOX_NETWORK:-none}"
AGENT_SANDBOX_MEMORY="${AGENT_SANDBOX_MEMORY:-2g}"
AGENT_SANDBOX_CPUS="${AGENT_SANDBOX_CPUS:-2.0}"
AGENT_SANDBOX_PIDS="${AGENT_SANDBOX_PIDS:-256}"
DOCKERFILE="${AGENT_SANDBOX_DOCKERFILE:-$REPO_ROOT/scripts/sandbox.Dockerfile}"

# Parse optional flags, then treat the rest as the research command.
#   build_agent_sandbox.sh [--network none|bridge] [--] <command...>
NET_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --network)     NET_OVERRIDE="$2"; shift 2 ;;
    --network=*)   NET_OVERRIDE="${1#--network=}"; shift ;;
    --)            shift; break ;;   # "--" only separates flags from command
    *)             break ;;           # first non-flag token starts the command
  esac
done
CMD=("$@")
if [ ${#CMD[@]} -eq 0 ]; then
  echo "usage: build_agent_sandbox.sh [--network none|bridge] -- <command...>" >&2
  exit 2
fi

# ---- ensure scratch exists ---------------------------------------------------
mkdir -p "$AGENT_SCRATCH_DIR"

# ---- authoritative isolation marker (written by the launcher, not the payload)
# The sandboxed program can forge anything inside its own result.json, so the
# ONLY trustworthy record of how it was executed is the one this host-side
# launcher writes before handing over control.
#
# The marker deliberately lives OUTSIDE $AGENT_SCRATCH_DIR when the caller passes
# AGENT_SANDBOX_MARKER_PATH: scratch is mounted rw into the container (and is the
# payload's cwd in soft mode), so a marker stored there would be writable by the
# very program whose confinement it attests to. The variable is scrubbed from the
# environment before the payload runs and is never forwarded into the container.
MARKER="${AGENT_SANDBOX_MARKER_PATH:-$AGENT_SCRATCH_DIR/.sandbox_mode}"
mkdir -p "$(dirname "$MARKER")" 2>/dev/null || true
write_marker() {
  local mode="$1" reason="$2"
  cat > "$MARKER" <<EOF
{"mode":"$mode","network":"$AGENT_SANDBOX_NETWORK","reason":"$reason","image":"$SANDBOX_IMAGE","require_hard":"${AGENT_SANDBOX_REQUIRE_HARD:-0}","ts":$(date +%s)}
EOF
}

use_soft() {
  local why="$1"
  # R9: fail closed when the caller demands real confinement.
  if [ "${AGENT_SANDBOX_REQUIRE_HARD:-0}" = "1" ]; then
    write_marker "unavailable" "$why"
    echo "[sandbox] FATAL: AGENT_SANDBOX_REQUIRE_HARD=1 but hard isolation is unavailable ($why)." >&2
    echo "[sandbox] Refusing to run unconfined on the host. Install/start Docker or unset the flag." >&2
    exit 78  # EX_CONFIG
  fi
  write_marker "soft" "$why"
  echo "[sandbox] $why -> SOFT isolation (host, no container)" >&2
  echo "[sandbox] WARNING: this is NOT a security boundary; results carry sandbox=soft." >&2
  # Translate container paths to host paths so the SAME command runs on the host
  # as it would in the container (/repo -> AGENT_REPO_DIR, /data -> AGENT_DATA_DIR).
  local -a translated=()
  for arg in "${CMD[@]}"; do
    case "$arg" in
      /repo/*)  arg="$AGENT_REPO_DIR/${arg#/repo/}" ;;
      /data/*)  arg="$AGENT_DATA_DIR/${arg#/data/}" ;;
    esac
    translated+=("$arg")
  done
  # Do not hand the payload the location of its own isolation attestation.
  unset AGENT_SANDBOX_MARKER_PATH
  AGENT_DATA_DIR="$AGENT_DATA_DIR" AGENT_SCRATCH_DIR="$AGENT_SCRATCH_DIR" \
    "${translated[@]}"
  exit $?
}

# ---- Docker availability check (fallback to soft isolation) ------------------
[ -n "$NET_OVERRIDE" ] && AGENT_SANDBOX_NETWORK="$NET_OVERRIDE"
[ "${AGENT_SANDBOX_DISABLE:-0}" = "1" ] && use_soft "AGENT_SANDBOX_DISABLE=1 set"
command -v docker >/dev/null 2>&1 || use_soft "docker CLI not found"
docker info >/dev/null 2>&1 || use_soft "docker daemon not reachable"

# ---- lazy image build --------------------------------------------------------
if ! docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
  echo "[sandbox] building image $SANDBOX_IMAGE ..." >&2
  docker build -t "$SANDBOX_IMAGE" -f "$DOCKERFILE" "$REPO_ROOT/scripts"
fi

# ---- run inside container ----------------------------------------------------
write_marker "hard" "docker run --network $AGENT_SANDBOX_NETWORK"
echo "[sandbox] HARD isolation: docker run --network $AGENT_SANDBOX_NETWORK" >&2
exec docker run --rm \
  --network "$AGENT_SANDBOX_NETWORK" \
  --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=128m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --memory "$AGENT_SANDBOX_MEMORY" \
  --cpus "$AGENT_SANDBOX_CPUS" \
  --pids-limit "$AGENT_SANDBOX_PIDS" \
  -v "$AGENT_DATA_DIR:/data:ro" \
  -v "$AGENT_REPO_DIR:/repo:ro" \
  -v "$AGENT_SCRATCH_DIR:/scratch:rw" \
  -e AGENT_DATA_DIR=/data \
  -e AGENT_SCRATCH_DIR=/scratch \
  "$SANDBOX_IMAGE" \
  "${CMD[@]}"
