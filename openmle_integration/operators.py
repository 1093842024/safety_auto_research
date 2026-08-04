"""Atomic MLE program-evolution operators (OpenMLE-Evo alignment, Phase B).

Each operator is a **pure function** (no side effects, no execution): it builds a
prompt and delegates code generation to an :class:`OperatorBackend`. Executing the
generated program is the caller's responsibility (via ``OpenMLETaskAdapter.step_task``
or the dual-loop executor) — exactly matching dojo's design where operators only
construct prompts and call the LLM, then hand execution back to ``Task.step_task``.

Two backend implementations ship:
  * :class:`TemplateOperatorBackend` — deterministic, **offline**; returns curated,
    known-good sklearn programs (and folds in failure feedback / combines parents as
    required). Used for tests and for any deployment without a wired LLM.
  * :class:`LLMOperatorBackend` — thin adapter over any ``(prompt: str) -> str``
    callable (e.g. an OpenAI / Codex / WorkBuddy client). Drop-in for real agentic
    generation; the four operators need no change.

Operators are inner-loop-only: every ``run_operator`` call passes through
:func:`assert_operator_inner_only`, so they can never be invoked from the frozen
external audit (``layer_11``) or the meta-loop (``layer_09``).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence, Tuple

from .contracts import VALID_SOLUTION, VALID_SOLUTION_FEEDBACK
from .inner_capability import assert_operator_inner_only


# --------------------------------------------------------------------------
# Prompt + backend protocol
# --------------------------------------------------------------------------
@dataclass
class OperatorPrompt:
    """Input handed to a backend for one operator call (faithful to dojo's prompt)."""

    operator: str  # draft | improve | debug | crossover
    task_description: str
    target: str = "Survived"
    id_col: str = "PassengerId"
    feature_cols: Tuple[str, ...] = ()
    current_program: Optional[str] = None  # improve / debug operate on this
    feedback: Optional[str] = None  # failure trace / mined mode (debug, improve)
    parent_programs: Tuple[str, ...] = ()  # crossover consumes >= 2 parents
    variant: int = 0  # draft diversity knob (explore the model space in the seed pop)


class OperatorBackend(Protocol):
    """Anything that turns a prompt into a program (the LLM in dojo's design)."""

    def generate(self, prompt: OperatorPrompt) -> str:
        ...


# --------------------------------------------------------------------------
# Deterministic offline backend -- produces valid, runnable sklearn programs
# --------------------------------------------------------------------------
def _header(target: str, id_col: str) -> str:
    return (
        "import os\n"
        "import numpy as np\n"
        "import pandas as pd\n"
        "from sklearn.compose import ColumnTransformer\n"
        "from sklearn.preprocessing import OneHotEncoder, StandardScaler\n"
        "from sklearn.impute import SimpleImputer\n"
        "from sklearn.pipeline import Pipeline\n"
        "from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier\n"
        "from sklearn.linear_model import LogisticRegression\n\n"
        "wd = os.environ['OPENMLE_WORKDIR']\n"
        f"train = pd.read_csv(os.path.join(wd, 'train.csv'))\n"
        f"eval_df = pd.read_csv(os.path.join(wd, 'eval.csv'))\n"
        f"target = {target!r}\n"
        f"id_col = {id_col!r}\n"
        "y = train[target]\n"
        "X = train.drop(columns=[target, id_col])\n"
        "X_eval = eval_df\n"
        "num = X.select_dtypes(include=[np.number]).columns.tolist()\n"
        "cat = X.select_dtypes(exclude=[np.number]).columns.tolist()\n"
        "pre = ColumnTransformer([\n"
        "    ('num', Pipeline([('imp', SimpleImputer(strategy='median')), ('sc', StandardScaler())]), num),\n"
        "    ('cat', Pipeline([('imp', SimpleImputer(strategy='most_frequent')),\n"
        "                      ('oh', OneHotEncoder(handle_unknown='ignore'))]), cat),\n"
        "])\n"
    )


def _fit_predict(tail: str) -> str:
    return (
        "clf.fit(X, y)\n"
        "preds = clf.predict(X_eval)\n"
        "sub = pd.DataFrame({id_col: eval_df[id_col].values, target: preds.astype(int)})\n"
        "sub.to_csv(os.path.join(wd, 'submission.csv'), index=False)\n"
        "print('done')\n"
    ) + tail


def _hash_int(text: Optional[str]) -> int:
    """Deterministic, content-derived seed so the offline operators vary their output
    by the parent / current program (instead of emitting byte-identical templates that
    the exact-code novelty filter would reject). Uses md5 (not ``hash()``) so the seed is
    stable across processes / runs.
    """

    return int(hashlib.md5((text or "").encode("utf-8")).hexdigest(), 16)


# Diverse base models for the seed population (the "model space" the Draft operator
# explores offline; a real LLM backend would explore architectures freely).
_DRAFT_MODELS = [
    "RandomForestClassifier(n_estimators=200, random_state=42)",
    "GradientBoostingClassifier(n_estimators=200, random_state=42)",
    "RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)",
    "LogisticRegression(max_iter=1000)",
]


def _draft_program(target: str, id_col: str, variant: int = 0) -> str:
    model = _DRAFT_MODELS[variant % len(_DRAFT_MODELS)]
    return _header(target, id_col) + (
        f"clf = Pipeline([('pre', pre), ('clf', {model})])\n"
    ) + _fit_predict("")


def _improve_program(
    target: str, id_col: str, feedback: Optional[str], current_program: Optional[str] = None
) -> str:
    # Vary the model family / capacity by the parent's identity (current_program) so
    # each improve step produces a distinct, parent-dependent program instead of a
    # byte-identical template (which the exact-code novelty filter would reject).
    h = _hash_int(current_program)
    family = (
        "GradientBoostingClassifier(n_estimators=300, learning_rate=0.05, random_state=42)",
        "RandomForestClassifier(n_estimators=300, max_depth=10, random_state=7)",
        "GradientBoostingClassifier(n_estimators=200, random_state=42)",
    )[h % 3]
    note = f"# improvement feedback: {TemplateOperatorBackend._sanitize_comment(feedback)}\n" if feedback else ""
    note += f"# improved-from parent(hash={h & 0xffff:04x})\n"
    return _header(target, id_col) + note + (
        f"clf = Pipeline([('pre', pre), ('clf', {family})])\n"
    ) + _fit_predict("")


def _debug_program(
    target: str, id_col: str, feedback: Optional[str], current_program: Optional[str] = None
) -> str:
    # Robust variant: wraps training in try/except and falls back to the majority
    # class. The parent's identity seeds the random_state so each debug step yields a
    # distinct (still-valid) program rather than a byte-identical template.
    h = _hash_int(current_program)
    rs = (0, 7, 42, 123)[h % 4]
    note = f"# debug feedback: {TemplateOperatorBackend._sanitize_comment(feedback)}\n" if feedback else ""
    note += f"# debugged-from parent(hash={h & 0xffff:04x})\n"
    return _header(target, id_col) + note + (
        "try:\n"
        f"    clf = Pipeline([('pre', pre), ('clf', RandomForestClassifier(n_estimators=200, random_state={rs}))])\n"
        "    clf.fit(X, y)\n"
        "    preds = clf.predict(X_eval)\n"
        "except Exception as exc:\n"
        "    maj = int(pd.Series(y).mode().iloc[0])\n"
        "    preds = np.full(len(X_eval), maj)\n"
        "sub = pd.DataFrame({id_col: eval_df[id_col].values, target: preds.astype(int)})\n"
        "sub.to_csv(os.path.join(wd, 'submission.csv'), index=False)\n"
        "print('done')\n"
    )


def _crossover_program(
    target: str, id_col: str, parent_programs: Tuple[str, ...] = ()
) -> str:
    # Blend two parent pipelines (RF + GBM) by averaging predicted probabilities -- a
    # deterministic offline analogue of dojo's Crossover. Both parents' identities seed
    # the two random_states, so different parent pairs yield distinct (novel) programs
    # instead of one byte-identical blend. A real LLM backend synthesizes from the
    # parents' actual source.
    h = _hash_int("\n---\n".join(parent_programs))
    rs1 = (0, 7, 42, 123)[h % 4]
    rs2 = (0, 7, 42, 123)[(h >> 4) % 4]
    parents_note = "".join(
        f"#   parent[{i}] src_len={len(p)}\n" for i, p in enumerate(parent_programs[:2])
    )
    return _header(target, id_col) + (
        f"# crossover of {len(parent_programs)} parent(s):\n{parents_note}"
        f"rf = Pipeline([('pre', pre), ('clf', RandomForestClassifier(n_estimators=200, random_state={rs1}))])\n"
        f"gbm = Pipeline([('pre', pre), ('clf', GradientBoostingClassifier(n_estimators=200, random_state={rs2}))])\n"
        "rf.fit(X, y); gbm.fit(X, y)\n"
        "p1 = rf.predict_proba(X_eval); p2 = gbm.predict_proba(X_eval)\n"
        "avg = (p1 + p2) / 2.0\n"
        "preds = np.argmax(avg, axis=1).astype(int)\n"
        "sub = pd.DataFrame({id_col: eval_df[id_col].values, target: preds})\n"
        "sub.to_csv(os.path.join(wd, 'submission.csv'), index=False)\n"
        "print('done')\n"
    )


class TemplateOperatorBackend:
    """Deterministic, offline operator backend (no LLM required)."""

    @staticmethod
    def _sanitize_comment(text: Optional[str]) -> str:
        """Make feedback safe to embed in a Python comment (strip newlines / quotes)."""

        if not text:
            return ""
        return text.replace("\n", " ").replace("\r", " ").replace("'", " ").replace('"', " ")

    def generate(self, prompt: OperatorPrompt) -> str:
        t, i = prompt.target, prompt.id_col
        if prompt.operator == "draft":
            return _draft_program(t, i, variant=prompt.variant)
        if prompt.operator == "improve":
            return _improve_program(t, i, prompt.feedback, prompt.current_program)
        if prompt.operator == "debug":
            return _debug_program(t, i, prompt.feedback, prompt.current_program)
        if prompt.operator == "crossover":
            return _crossover_program(t, i, prompt.parent_programs)
        raise ValueError(f"unknown operator {prompt.operator!r}")


class LLMOperatorBackend:
    """Adapter over any ``(prompt: str) -> str`` callable (real LLM agent)."""

    def __init__(self, generate_fn: Callable[[str], str]) -> None:
        self._fn = generate_fn

    def generate(self, prompt: OperatorPrompt) -> str:
        text = (
            f"# operator={prompt.operator}\n"
            f"# task: {prompt.task_description}\n"
            f"# target={prompt.target} id_col={prompt.id_col}\n"
        )
        if prompt.current_program:
            text += f"# current_program:\n{prompt.current_program}\n"
        if prompt.feedback:
            text += f"# feedback:\n{prompt.feedback}\n"
        if prompt.parent_programs:
            for idx, p in enumerate(prompt.parent_programs, 1):
                text += f"# parent_program_{idx}:\n{p}\n"
        text += "# Return a complete python program writing submission.csv.\n"
        return self._fn(text)


class LocalLLMOperatorBackend(LLMOperatorBackend):
    """Operator backend backed by a **locally-trained** model (Phase D2).

    Accepts either:
      * a ``generate(text) -> str`` callable -- e.g. the object returned by
        ``LocalLLMTrainer.export_generator()``; or
      * a trainer-like object exposing ``export_generator()`` (duck-typed, so no
        import of ``local_train`` is needed here -- avoids a circular import).

    Prompt formatting is inherited verbatim from :class:`LLMOperatorBackend`.
    """

    def __init__(self, source) -> None:
        if hasattr(source, "export_generator") and callable(source.export_generator):
            generate_fn = source.export_generator()
        else:
            generate_fn = source
        if not callable(generate_fn):
            raise TypeError("LocalLLMOperatorBackend needs a generate callable or a trainer")
        super().__init__(generate_fn)


def make_local_operator_backend(trainer) -> LocalLLMOperatorBackend:
    """Convenience factory: trainer -> :class:`LocalLLMOperatorBackend`."""

    return LocalLLMOperatorBackend(trainer)


# --------------------------------------------------------------------------
# Third-party OpenAI-compatible chat backend (Phase D-Ext1)
# --------------------------------------------------------------------------
DEFAULT_API_SYSTEM_PROMPT = (
    "You are an expert Kaggle-style ML engineer. Given a task spec and an operator "
    "instruction, output a single complete, runnable Python program. The program "
    "reads train.csv/eval.csv from os.environ['OPENMLE_WORKDIR'], trains a "
    "scikit-learn pipeline, predicts on eval.csv, and writes submission.csv "
    "(columns: id_col, target). Respond with code only -- no markdown fences, no "
    "explanatory prose."
)


def _strip_code_fences(text: str) -> str:
    """If the model wrapped the program in ```python ... ```, return the bare code."""

    if not text:
        return text
    s = text.strip()
    m = re.match(r"^```[a-zA-Z0-9]*\n(.*?)\n```$", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    # also handle a leading fence without trailing newline
    if s.startswith("```"):
        s = s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.split("\n", 1)[1] if "\n" in s else s
        return s.strip()
    return s


@dataclass
class ApiLLMConfig:
    """Configuration for a third-party OpenAI-compatible chat/completions endpoint.

    Every field is env-overridable so the proxy can be wired without code changes:

      * ``OPENMLE_API_BASE_URL``  -- chat/completions URL (default: empty, must be set)
      * ``OPENMLE_API_API_KEY``   -- bearer token
      * ``OPENMLE_API_MODEL``     -- model name (default: deepseek-v4-flash-official)
      * ``OPENMLE_API_TME_OPEN``  -- set to 1/true/yes to add the ``TmeOpenApi: true`` header
    """

    base_url: str = field(
        default_factory=lambda: os.getenv(
            "OPENMLE_API_BASE_URL", ""
        )
    )
    api_key: str = field(default_factory=lambda: os.getenv("OPENMLE_API_API_KEY", ""))
    model: str = field(
        default_factory=lambda: os.getenv("OPENMLE_API_MODEL", "deepseek-v4-flash-official")
    )
    temperature: float = 0.6
    max_tokens: int = 4000
    top_p: float = 0.7
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    extra_headers: dict = field(default_factory=dict)
    timeout: float = 120.0
    system_prompt: str = DEFAULT_API_SYSTEM_PROMPT
    stream: bool = False

    @staticmethod
    def from_env() -> "ApiLLMConfig":
        tme = os.getenv("OPENMLE_API_TME_OPEN", "").lower() in ("1", "true", "yes")
        extra = {"TmeOpenApi": "true"} if tme else {}
        return ApiLLMConfig(
            base_url=os.getenv(
                "OPENMLE_API_BASE_URL", ""
            ),
            api_key=os.getenv("OPENMLE_API_API_KEY", ""),
            model=os.getenv("OPENMLE_API_MODEL", "deepseek-v4-flash-official"),
            extra_headers=extra,
        )


class ApiLLMOperatorBackend:
    """Operator backend backed by any OpenAI-compatible chat/completions API.

    Output code fences are stripped so the four operators receive raw python. The
    prompt text is formatted identically to :class:`LLMOperatorBackend` so behaviour
    is consistent across local / API / template backends.

    Example (TME llmproxy)::

        cfg = ApiLLMConfig(api_key="<bearer>", extra_headers={"TmeOpenApi": "true"})
        backend = ApiLLMOperatorBackend(cfg)
        program = draft_program(backend, target="Survived", id_col="PassengerId",
                                 task_description="classify titanic")
    """

    def __init__(self, config: Optional[ApiLLMConfig] = None) -> None:
        self.cfg = config or ApiLLMConfig.from_env()

    @staticmethod
    def _format(prompt: OperatorPrompt) -> str:
        text = (
            f"# operator={prompt.operator}\n"
            f"# task: {prompt.task_description}\n"
            f"# target={prompt.target} id_col={prompt.id_col}\n"
        )
        if prompt.feature_cols:
            text += f"# features={list(prompt.feature_cols)}\n"
        if prompt.current_program:
            text += f"# current_program:\n{prompt.current_program}\n"
        if prompt.feedback:
            text += f"# feedback:\n{prompt.feedback}\n"
        if prompt.parent_programs:
            for idx, p in enumerate(prompt.parent_programs, 1):
                text += f"# parent_program_{idx}:\n{p}\n"
        text += "# Return ONLY a complete python program writing submission.csv. No prose.\n"
        return text

    def _to_messages(self, prompt: OperatorPrompt) -> list[dict]:
        return [
            {"role": "system", "content": self.cfg.system_prompt},
            {"role": "user", "content": self._format(prompt)},
        ]

    def generate(self, prompt: OperatorPrompt) -> str:
        import json as _json
        import urllib.error as _err
        import urllib.request as _req

        body = {
            "model": self.cfg.model,
            "messages": self._to_messages(prompt),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "top_p": self.cfg.top_p,
            "frequency_penalty": self.cfg.frequency_penalty,
            "presence_penalty": self.cfg.presence_penalty,
            "stream": self.cfg.stream,
        }
        data = _json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }
        headers.update(self.cfg.extra_headers)
        req = _req.Request(self.cfg.base_url, data=data, headers=headers, method="POST")
        try:
            with _req.urlopen(req, timeout=self.cfg.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except _err.HTTPError as e:  # HTTP 4xx/5xx
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"API {e.code} {e.reason}: {detail}") from e
        except _err.URLError as e:  # DNS / connection refused / timeout
            raise RuntimeError(f"API connection failed: {e.reason}") from e
        obj = _json.loads(raw)
        content = obj["choices"][0]["message"]["content"]
        return _strip_code_fences(content)


def make_api_backend(
    api_key: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    tme_open: bool = False,
    **overrides,
) -> ApiLLMOperatorBackend:
    """Convenience factory for the third-party API backend.

    ``tme_open=True`` adds the ``TmeOpenApi: true`` header required by the TME proxy.
    Any extra ``ApiLLMConfig`` field can be overridden via keyword args.
    """

    cfg = ApiLLMConfig.from_env()
    if api_key is not None:
        cfg.api_key = api_key
    if base_url is not None:
        cfg.base_url = base_url
    if model is not None:
        cfg.model = model
    if tme_open:
        cfg.extra_headers = {**cfg.extra_headers, "TmeOpenApi": "true"}
    _ALLOWED_OVERRIDES = {
        "temperature", "max_tokens", "top_p", "frequency_penalty",
        "presence_penalty", "timeout", "system_prompt", "stream",
    }
    for k, v in overrides.items():
        if k not in _ALLOWED_OVERRIDES:
            raise ValueError(
                f"unknown config field {k!r}; allowed: {sorted(_ALLOWED_OVERRIDES)}"
            )
        setattr(cfg, k, v)
    return ApiLLMOperatorBackend(cfg)


# --------------------------------------------------------------------------
# Operator dispatch (the seam the inner loop calls)
# --------------------------------------------------------------------------
def run_operator(
    operator: str,
    backend: OperatorBackend,
    *,
    task_description: str,
    target: str = "Survived",
    id_col: str = "PassengerId",
    feature_cols: Sequence[str] = (),
    current_program: Optional[str] = None,
    feedback: Optional[str] = None,
    parent_programs: Sequence[str] = (),
    variant: int = 0,
    caller_stage: Optional[str] = None,
) -> str:
    """Run one atomic operator and return the generated program (inner-loop only)."""

    assert_operator_inner_only(operator, caller_stage)
    prompt = OperatorPrompt(
        operator=operator,
        task_description=task_description,
        target=target,
        id_col=id_col,
        feature_cols=tuple(feature_cols),
        current_program=current_program,
        feedback=feedback,
        parent_programs=tuple(parent_programs),
        variant=variant,
    )
    return backend.generate(prompt)


# Convenience wrappers (each pure, each inner-loop-only).
def draft_program(backend, *, target, id_col, task_description, variant: int = 0,
                  caller_stage=None) -> str:
    return run_operator("draft", backend, task_description=task_description,
                        target=target, id_col=id_col, variant=variant, caller_stage=caller_stage)


def improve_program(backend, *, target, id_col, task_description, current_program,
                    feedback=None, caller_stage=None) -> str:
    return run_operator("improve", backend, task_description=task_description,
                        target=target, id_col=id_col, current_program=current_program,
                        feedback=feedback, caller_stage=caller_stage)


def debug_program(backend, *, target, id_col, task_description, current_program,
                  feedback=None, caller_stage=None) -> str:
    return run_operator("debug", backend, task_description=task_description,
                        target=target, id_col=id_col, current_program=current_program,
                        feedback=feedback, caller_stage=caller_stage)


def crossover_program(backend, *, target, id_col, task_description, parent_programs,
                      caller_stage=None) -> str:
    return run_operator("crossover", backend, task_description=task_description,
                        target=target, id_col=id_col, parent_programs=parent_programs,
                        caller_stage=caller_stage)


# --------------------------------------------------------------------------
# Phase B3 seams: Debug <-> failure_miner, Crossover <-> StrategyArchive
# --------------------------------------------------------------------------
def build_debug_feedback(failed_outcome: dict, events=None) -> str:
    """Build the feedback string for the Debug operator.

    Prefers a mined recurring failure mode from the run's event log (failure_miner);
    falls back to the program's own ``VALID_SOLUTION_FEEDBACK``. Never touches the
    curated outer-audit input -- isolation invariant preserved.
    """

    if events:
        try:
            from ..control_plane.failure_miner import mine_failure_modes
        except ImportError:
            mine_failure_modes = None
        if mine_failure_modes is not None:
            try:
                modes = mine_failure_modes(list(events), top_n=2)
                if modes:
                    return "; ".join(m["mode"] for m in modes)
            except Exception:
                import logging
                logging.warning("failure_miner failed for debug feedback", exc_info=True)
    return failed_outcome.get(VALID_SOLUTION_FEEDBACK) or "program failed to produce a valid submission"


def select_crossover_parents(archive, run_id: str, k: int = 2, maximize: bool = True):
    """Pick ``k`` evaluated program candidates (with code) to crossover.

    Reads only from the run-scoped EvolutionArchive (program nodes) -- the local
    analogue of drawing parents from the StrategyArchive / search tree. Isolation
    invariant #3 (run_id filtering) is enforced by the archive query.

    ``maximize=True`` sorts highest fitness first (typical for accuracy);
    ``maximize=False`` sorts lowest first (for lower-is-better metrics).
    """

    progs = [c for c in archive.list_by_kind(run_id, "program")
             if c.code and c.fitness is not None]
    progs.sort(key=lambda c: c.fitness, reverse=maximize)
    return progs[:k]
