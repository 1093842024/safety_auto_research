"""Checkpoint schema — the minimal sufficient set to resume a run (contract 3).

This module **freezes the schema and the atomic IO** for D1; it does NOT wire
resume into the orchestrator (that is #1, scheduled for D4). Defining the shape
now lets member A build #1 and member B build the WebUI/report against one
spec, and pins the seams with the other two contracts:

* config → checkpoint: the resolved, **redacted** config snapshot
  (``config_schema.redacted_snapshot``) is what a checkpoint references; secrets
  never land here.
* event → checkpoint: :data:`pending_user` mirrors the ``AWAIT_USER`` payload
  (:class:`events.payloads.AwaitUser`), so a run paused on a human question can
  be restored mid-question. Writing a checkpoint emits ``CHECKPOINT_SAVED``.

Writes are atomic (temp file + ``os.replace``) so an interrupt never leaves a
half-written checkpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .._app import CONFIG_DIR_NAME

#: Bump when the on-disk shape changes incompatibly. Readers reject unknown
#: major versions rather than silently mis-parsing an old/newer file.
SCHEMA_VERSION = 1

#: Conventional location, relative to the workspace/agent dir.
DEFAULT_CHECKPOINT_DIR = f"{CONFIG_DIR_NAME}/checkpoint"
DEFAULT_CHECKPOINT_NAME = "checkpoint.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GitState(BaseModel):
    """Git topology needed to re-attach a run to its branches/worktrees."""

    trunk_branch: str | None = None
    active_branches: list[str] = Field(default_factory=list)
    worktrees: list[str] = Field(default_factory=list)


class InflightExecutor(BaseModel):
    """A executor that was running when the checkpoint was taken.

    On resume these are treated as **interrupted** and re-queued (#1); they are
    never assumed to have completed.
    """

    node_id: str
    branch: str
    started_at: str = Field(default_factory=_utc_now_iso)  # ISO8601


class CacheAnchor(BaseModel):
    """KV-cache prefix anchors (#13), so a resumed run keeps cache stability."""

    prefix_anchor_hash: str | None = None
    stable_system_hash: str | None = None


class Checkpoint(BaseModel):
    """The minimal sufficient set to resume one research run."""

    schema_version: int = SCHEMA_VERSION
    run_name: str
    created_at: str = Field(default_factory=_utc_now_iso)  # ISO8601
    cycle_num: int = 0
    phase: str = "init"

    # Idea tree — referenced by path to stay consistent with the live file
    # rather than duplicating it.
    tree_path: str = ".coordinator/idea_tree.json"
    # Coordinator conversation history, replayed to rebuild reasoning/context.
    messages_path: str = "checkpoint/messages.jsonl"

    git: GitState = Field(default_factory=GitState)
    inflight_executors: list[InflightExecutor] = Field(default_factory=list)
    cache: CacheAnchor = Field(default_factory=CacheAnchor)

    # Suspended human-in-the-loop state; mirrors the AWAIT_USER payload so a run
    # paused on a question can resume exactly there. None = not waiting.
    pending_user: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for serialization."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        """Validate an on-disk mapping, rejecting incompatible versions.

        Policy: a newer major version is rejected; an older/equal one is
        accepted. Any incompatible shape change MUST bump ``SCHEMA_VERSION``
        — unknown keys are dropped on load (pydantic ``extra='ignore'``), so an
        unversioned additive change would be silently lost on a read-then-write.
        """
        version = data.get("schema_version")
        if version is not None:
            try:
                version_num = int(version)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid checkpoint schema_version: {version!r}") from exc
            if version_num > SCHEMA_VERSION:
                raise UnsupportedCheckpointVersion(
                    f"checkpoint schema_version {version} is newer than supported {SCHEMA_VERSION}"
                )
        return cls.model_validate(data)


class UnsupportedCheckpointVersion(RuntimeError):
    """Raised when a checkpoint file is a newer, unreadable schema version."""


def default_checkpoint_path(base_dir: str | os.PathLike[str]) -> Path:
    """Return the conventional checkpoint path under ``base_dir``."""
    return Path(base_dir) / DEFAULT_CHECKPOINT_NAME


def write_checkpoint(
    path: str | os.PathLike[str],
    checkpoint: Checkpoint,
    *,
    reason: str = "manual",
    bus: Any | None = None,
) -> Path:
    """Atomically write ``checkpoint`` to ``path`` (temp file + ``os.replace``).

    Emits ``CHECKPOINT_SAVED`` on ``bus`` when one is provided, tying this
    contract to the event contract. Returns the final path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(checkpoint.to_dict(), indent=2, ensure_ascii=False)

    # Unique temp file in the SAME directory (so os.replace is atomic on the
    # same filesystem) — concurrent writers never share a temp name. fsync
    # before replace so a power loss can't leave a torn file. Clean up the temp
    # on any failure before the rename succeeds.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)  # atomic rename on the same filesystem
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    if bus is not None:
        from .. import events  # local import to avoid a hard events dependency

        bus.emit(
            events.types.CHECKPOINT_SAVED,
            {"path": str(path), "cycle": checkpoint.cycle_num, "reason": reason},
        )
    return path


def read_checkpoint(path: str | os.PathLike[str]) -> Checkpoint | None:
    """Load a checkpoint, or ``None`` if the file does not exist.

    Raises :class:`UnsupportedCheckpointVersion` for a too-new schema and
    ``ValueError`` for a malformed file.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt checkpoint at {path}: {exc}") from exc
    return Checkpoint.from_dict(data)


# ---------------------------------------------------------------------------
# Message history (replayed on resume to rebuild the reasoning chain, #1)
# ---------------------------------------------------------------------------
#
# The agent's ``messages`` are already plain JSON-serializable dicts in every
# provider's native format (``LLMResponse.raw_content`` is ``list[dict]``), so
# they persist and replay without a serialization shim.


def write_messages(
    path: str | os.PathLike[str], messages: list[dict[str, Any]]
) -> Path:
    """Atomically write ``messages`` as JSONL (one message dict per line)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(m, ensure_ascii=False) + "\n" for m in messages
    )

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_messages(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load a JSONL message history, or ``[]`` if the file does not exist.

    Tolerant of corruption: a truncated final line (interrupted write) or a
    malformed line is skipped rather than aborting the whole resume — the
    surviving messages are still worth replaying. ``write_messages`` writes
    atomically, so corruption is rare, but a partial line must never turn a
    resumable run into a hard failure.
    """
    path = Path(path)
    if not path.is_file():
        return []
    messages: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # skip a corrupt / truncated line, keep the rest
    return messages


def seal_interrupted_tail(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make a replayed history safe to continue.

    If the run was interrupted mid-turn, the last message can be an assistant
    turn whose ``tool_use`` blocks never received results. Most providers
    reject a history where a ``tool_use`` is not answered by a ``tool_result``,
    so append a synthetic ``user`` message answering each dangling call with an
    error result. No-op when the tail is already a safe boundary.
    """
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "assistant":
        return messages
    content = last.get("content")
    if not isinstance(content, list):
        return messages
    tool_uses = [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    if not tool_uses:
        return messages
    results = [
        {
            "type": "tool_result",
            "tool_use_id": b.get("id"),
            "content": "[interrupted by checkpoint/resume — not executed]",
            "is_error": True,
        }
        for b in tool_uses
    ]
    return messages + [{"role": "user", "content": results}]
