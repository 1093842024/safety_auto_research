"""Dependency-light Python interpreter (Phase A).

Mirrors ``dojo.core.interpreters.python.PythonInterpreter`` semantics (run code in an
isolated process, capture stdout/stderr, enforce a timeout, summarise exceptions) but
without any heavy upstream dependencies. The agent's "program" (action) is expected to
write a ``submission.csv`` to the working directory; we surface that path so the task
can validate + score it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import traceback
from typing import Any, Dict, Optional

from .contracts import ExecutionResult, Interpreter

# Keys that look like secrets / credentials. Model-generated programs must NEVER
# receive them — passing the full host environment to an untrusted subprocess is an
# RCE + secret-leak vector (C1). We strip any such key and only forward a minimal,
# explicitly-allowed env to the child (plus OPENMLE_WORKDIR).
_SECRET_KEY_HINTS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PWD", "API", "CRED",
    "AUTH", "PRIVATE", "CERT", "BEARER", "SESSION", "COOKIE", "OPENAI",
    "ANTHROPIC", "GEMINI", "AZURE", "AWS", "GCP", "DB_", "DATABASE", "SQL",
    "REDIS", "MONGO", "HF_", "HUGGING", "LLM_JUDGE", "WEBHOOK", "SLACK",
)
# Baseline env keys that are safe (and often required) to forward.
_SAFE_ENV_KEYS = (
    "PATH", "PYTHONPATH", "LANG", "LC_ALL", "LANGUAGE", "HOME", "USER",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "COMSPEC",
    "LD_LIBRARY_PATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NUMEXPR_MAX_THREADS",
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "TF_CPP_MIN_LOG_LEVEL", "TF_FORCE_GPU_ALLOW_GROWTH",
    "OPENMLE_MEM_LIMIT_MB",  # opt-in memory cap read by _child_resource_limits
)


def _safe_env(extra: Dict[str, str] | None = None) -> Dict[str, str]:
    """Build a minimal environment for the untrusted child process.

    Forward only non-secret, explicitly-allowed keys (a small allowlist wins over a
    deny-list for secrets). The child is an LLM-generated program and must not be able
    to exfiltrate host credentials via ``os.environ``.
    """

    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if any(h in ku for h in _SAFE_ENV_KEYS):
            env[k] = v
            continue
        # Last-resort deny-list: drop anything that even smells like a secret.
        if any(h in ku for h in _SECRET_KEY_HINTS):
            continue
        # Allow other innocuous-looking vars (e.g. custom non-secret config) through.
        env[k] = v
    # Allow callers (and the harness) to inject a few explicit, non-secret vars.
    if extra:
        for k, v in extra.items():
            if not any(h in k.upper() for h in _SECRET_KEY_HINTS):
                env[k] = v
    return env


def _child_resource_limits() -> None:
    """Apply CPU / process-count limits in the child (preexec_fn).

    Wrapped so it can NEVER raise into the child (a raise would fail the subprocess).
    Memory cap is opt-in via OPENMLE_MEM_LIMIT_MB to avoid breaking legitimate ML
    workloads by default. NOTE: this is defence-in-depth only — the real boundary is a
    container / gVisor / Firecracker sandbox with an isolated network namespace, which
    production deployments MUST add on top of this.
    """

    try:
        import resource  # Unix-only; ImportError is caught below.

        # Cap CPU seconds (mirrors the wall-clock timeout but enforced by the kernel).
        _safe_setrlimit(resource.RLIMIT_CPU, (600, 600))
        # Cap the number of child processes the program can fork (anti-fork-bomb).
        _safe_setrlimit(resource.RLIMIT_NPROC, (128, 128))
        mem_mb = int(os.environ.get("OPENMLE_MEM_LIMIT_MB", "0") or "0")
        if mem_mb > 0:
            _safe_setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
    except Exception:
        # Any failure to apply limits must NOT abort the run — leave limits unset.
        pass


def _safe_setrlimit(resource_id: Any, limits: Any) -> None:
    try:
        import resource

        resource.setrlimit(resource_id, limits)  # type: ignore[arg-type]
    except (ValueError, OSError):
        pass


class PythonInterpreter(Interpreter):
    """Executes a program string in a subprocess (mirror dojo semantics)."""

    def __init__(
        self,
        python_executable: Optional[str] = None,
        timeout: float = 600.0,
        workdir: Optional[str] = None,
    ) -> None:
        # Use the same interpreter that runs this project so sklearn/numpy are available.
        self.python_executable = python_executable or sys.executable
        # Fast smoke test: ensure the target interpreter can import required packages.
        try:
            subprocess.run(
                [self.python_executable, "-c", "import sklearn, pandas, numpy"],
                capture_output=True, timeout=30, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(
                f"{self.python_executable} cannot import sklearn/pandas/numpy: {e}"
            ) from e
        self.timeout = timeout
        self._tmp = tempfile.TemporaryDirectory() if workdir is None else None
        self.workdir = workdir or self._tmp.name  # type: ignore[union-attr]
        os.makedirs(self.workdir, exist_ok=True)

    def run(self, code: str, file_name: str = "solution.py") -> ExecutionResult:
        path = os.path.join(self.workdir, file_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        # C1 fix: NEVER pass the full host environment to untrusted model-generated
        # code. Forward only a minimal, secret-stripped env.
        env = _safe_env({"OPENMLE_WORKDIR": self.workdir})
        try:
            proc = subprocess.run(
                [self.python_executable, path],
                cwd=self.workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                # New session so we can cleanly signal the whole group; rlimits applied
                # inside the child as a kernel-enforced safety net.
                start_new_session=True,
                preexec_fn=_child_resource_limits,
            )
            return ExecutionResult(
                term_out=proc.stdout + proc.stderr,
                exec_time=0.0,
                exit_code=proc.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Kill the whole process group — start_new_session=True placed the child in
            # its own session, so killpg also reaps any grandchildren the untrusted
            # program forked (otherwise they orphan and keep consuming host resources).
            try:
                if proc.pid is not None:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            out = (exc.stdout or "") + (exc.stderr or "")
            return ExecutionResult(term_out=out, exec_time=self.timeout, exit_code=-1, timed_out=True)
        except Exception:  # pragma: no cover - defensive
            return ExecutionResult(
                term_out=traceback.format_exc(), exec_time=0.0, exit_code=-2, timed_out=False
            )

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def sanitize_execution_output_for_prompt(out: str, limit: int = 4000) -> str:
    """Trim/clean execution output before injecting into an LLM prompt (mirror dojo util)."""
    if not out:
        return ""
    return textwrap.shorten(out, width=limit, placeholder="\n...[truncated]...")


def exception_summary(out: str) -> str:
    """Return the last traceback line / error summary for feedback (mirror dojo)."""
    lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""
