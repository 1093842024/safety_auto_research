"""Dependency-light Python interpreter (Phase A).

Mirrors ``dojo.core.interpreters.python.PythonInterpreter`` semantics (run code in an
isolated process, capture stdout/stderr, enforce a timeout, summarise exceptions) but
without any heavy upstream dependencies. The agent's "program" (action) is expected to
write a ``submission.csv`` to the working directory; we surface that path so the task
can validate + score it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import traceback
from typing import Any, Dict, Optional

from .contracts import ExecutionResult, Interpreter


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
        self.timeout = timeout
        self._tmp = tempfile.TemporaryDirectory() if workdir is None else None
        self.workdir = workdir or self._tmp.name  # type: ignore[union-attr]
        os.makedirs(self.workdir, exist_ok=True)

    def run(self, code: str, file_name: str = "solution.py") -> ExecutionResult:
        path = os.path.join(self.workdir, file_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        env = dict(os.environ)
        env["OPENMLE_WORKDIR"] = self.workdir
        try:
            proc = subprocess.run(
                [self.python_executable, path],
                cwd=self.workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return ExecutionResult(
                term_out=proc.stdout + proc.stderr,
                exec_time=0.0,
                exit_code=proc.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
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
