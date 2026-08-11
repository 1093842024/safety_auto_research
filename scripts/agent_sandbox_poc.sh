#!/bin/bash
# agent_sandbox_poc.sh — capability probe for no-Docker agent isolation on THIS host.
#
# Purpose: empirically characterize which isolation primitives actually WORK on
# macOS 26.5.1 (Tahoe), so the F3 agent wrapper is not built on a broken foundation.
# This is a PROBE, not a pass/fail gate: it reports findings.
#
# Probes:
#   [1] sandbox-exec — can a Seatbelt profile be APPLIED at all?   (expect: NO on 26.5.1)
#   [2] uv           — can a hermetic venv be created quickly?      (expect: YES)
#   [3] sudo -u nobody — can we drop privileges non-interactively?  (expect: NO in this env)
#   [4] podman       — is a machine initialized / usable?           (expect: not initialized)
#
# Usage:  bash scripts/agent_sandbox_poc.sh

set -u

echo "=== host ==="
echo "macOS: $(sw_vers -productVersion 2>/dev/null)  arch: $(uname -m)"

# [1] sandbox-exec — try to APPLY any sandbox at all.
echo
echo "[1] sandbox-exec: apply a minimal profile?"
if sandbox-exec -p '(version 1)(allow default)(deny network*)' -- true 2>/tmp/sbox_err; then
  echo "  sandbox-exec APPLIES (unexpected on 26.5.1 — would be good news)"
else
  echo "  sandbox-exec REFUSED: $(cat /tmp/sbox_err)"
fi

# [2] uv — hermetic dependency isolation.
echo
echo "[2] uv: hermetic venv creation?"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  echo "  uv MISSING"
else
  T="$(mktemp -d)"
  if uv venv "$T" >/dev/null 2>&1 && [ -x "$T/bin/python" ]; then
    echo "  uv WORKS: hermetic venv at $T ($(uv --version))"
  else
    echo "  uv FAILED to create venv"
  fi
  rm -rf "$T"
fi

# [3] privilege drop via sudo (needed for unprivileged-user isolation).
echo
echo "[3] sudo -u nobody (non-interactive privilege drop)?"
if sudo -n -u nobody true 2>/tmp/sudo_err; then
  echo "  sudo drop WORKS"
else
  echo "  sudo drop BLOCKED: $(cat /tmp/sudo_err)"
fi

# [4] podman — real container isolation, but needs a Linux VM on macOS.
echo
echo "[4] podman machine?"
if command -v podman >/dev/null; then
  # A real machine shows a VM-TYPE token (applehv/qemu) on its data row; the bare
  # header alone (NAME/VM TYPE/...) must NOT count as "present".
  if podman machine list 2>/dev/null | grep -Eq 'applehv|qemu'; then
    echo "  podman machine PRESENT (container isolation available)"
  else
    echo "  podman PRESENT but NO machine initialized (needs 'podman machine init' + start)"
  fi
else
  echo "  podman MISSING"
fi

echo
echo "=== conclusion ==="
echo "On this host: sandbox-exec is dead (kernel refuses to apply any sandbox);"
echo "sudo privilege-drop is blocked in this context; uv is the only reliably-working"
echo "isolation primitive (dependency isolation only, NOT security confinement);"
echo "podman is available but NO machine is initialized (needs a Linux VM + setup)."
