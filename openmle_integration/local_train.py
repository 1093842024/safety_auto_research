"""Phase D2 -- Local lightweight-LLM training on Apple Silicon (CPU / MPS).

This module trains a **small causal LM (<= 0.6B params)** directly on the Mac, using
either:

  * ``model_source="hf"``     -- a HuggingFace repo (e.g. ``Qwen/Qwen2.5-0.5B-Instruct``)
                                 fine-tuned with PEFT/LoRA. Needs ``transformers``,
                                 ``peft``, ``accelerate`` (lazy-imported; not installed by
                                 default). Best for real operator backends.
  * ``model_source="synthetic"`` -- a tiny in-code char-LM (no network, no HF weights).
                                 Used for self-tests / demos to prove the MPS training
                                 loop end-to-end on this machine, and as a safe fallback
                                 when no model checkpoint is available.

Key Mac facts handled here:
  * **Device auto-selection** -- ``detect_device()`` returns ``"mps"`` on Apple Silicon
    (Metal) and falls back to ``"cpu"`` otherwise. Training tensors are moved to it.
  * **Memory budget** -- LoRA keeps the base model frozen so a 0.5B model fits in RAM;
    ``max_params_b`` guards against accidentally loading a too-large checkpoint.
  * **Lazy imports** -- ``torch`` / ``transformers`` / ``peft`` are imported inside the
    functions that need them, so this module is importable (and unit-testable for its
    pure logic) even when the heavy stacks are absent.

The trained model is exported as a ``generate(text) -> str`` callable that drops
straight into :class:`~safety_auto_research.openmle_integration.operators.LLMOperatorBackend`,
so the four atomic operators (Draft/Improve/Debug/Crossover) can be driven by a model
fine-tuned locally -- closing the loop from program evolution (Phase C) to a real,
on-device generator.
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple

# A small, open, <=0.6B instruct model that runs comfortably on Apple Silicon with LoRA.
DEFAULT_MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"

# Printable ASCII vocabulary for the synthetic char-LM (plus 4 control tokens).
_CTRL = ["<pad>", "<bos>", "<eos>", "<unk>"]
_CHAR_VOCAB = _CTRL + [chr(c) for c in range(32, 127)]
_CHAR2IDX = {ch: i for i, ch in enumerate(_CHAR_VOCAB)}


# --------------------------------------------------------------------------
# Device selection
# --------------------------------------------------------------------------
def detect_device(prefer: str = "mps") -> str:
    """Return the best training device for this Mac.

    ``"mps"`` when Metal is available (Apple Silicon), else ``"cpu"``. Resilient to
    torch being absent (returns ``"cpu"`` so callers can still build configs).
    """

    try:
        import torch
    except Exception:
        return "cpu"
    mps = getattr(torch.backends, "mps", None)
    if prefer == "mps" and mps is not None and mps.is_available():
        return "mps"
    return "cpu"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass
class TrainConfig:
    model_id: str = DEFAULT_MODEL_ID
    model_source: str = "hf"  # "hf" | "synthetic"
    max_params_b: float = 0.6  # refuse to load a model larger than this (guard)
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    epochs: int = 1
    batch_size: int = 1
    lr: float = 2e-4
    max_seq_len: int = 512
    warmup_steps: int = 0
    gradient_accumulation_steps: int = 1
    device: Optional[str] = None  # auto if None
    output_dir: str = "data/openmle_local_train"
    n_samples: int = 64  # size of the synthetic (teacher-generated) dataset
    seed: int = 42
    report_every: int = 5

    def effective_device(self) -> str:
        return self.device or detect_device()


# --------------------------------------------------------------------------
# Synthetic char-LM (no network, no HF) -- proves MPS training on-device
# --------------------------------------------------------------------------
class _CharTokenizer:
    def __init__(self, vocab: Sequence[str]) -> None:
        self.vocab = list(vocab)
        self.v2i = {ch: i for i, ch in enumerate(self.vocab)}

    def encode(self, text: str) -> list[int]:
        return [self.v2i.get(ch, self.v2i["<unk>"]) for ch in text]

    def decode(self, ids: Sequence[int]) -> str:
        return "".join(self.vocab[i] for i in ids if 0 <= i < len(self.vocab))

    @property
    def vocab_size(self) -> int:
        return len(self.vocab)


def _build_tiny_lm(vocab_size: int, d_model: int = 128, n_layers: int = 2):
    """Define + return the tiny char-LM class.

    ``torch`` is imported **here**, inside the factory, so the module stays
    importable (and its pure logic unit-testable) even when torch is not installed.
    The class subclasses ``torch.nn.Module`` so ``.to()`` / ``.train()`` /
    ``.parameters()`` / ``.state_dict()`` all behave natively.
    """

    import torch
    import torch.nn as nn

    class _TinyCharLM(nn.Module):
        def __init__(self):
            super().__init__()
            self.d_model = d_model
            self.emb = nn.Embedding(vocab_size, d_model)
            self.lstm = nn.LSTM(d_model, d_model, n_layers, batch_first=True, dropout=0.1)
            self.head = nn.Linear(d_model, vocab_size)
            self.max_len = 512

        def forward(self, input_ids):
            x = self.emb(input_ids)
            h, _ = self.lstm(x)
            return self.head(h)

        @torch.no_grad()
        def generate(self, input_ids, max_new_tokens: int = 64, eos_id: int = 2, device: str = "cpu"):
            self.eval()
            ids = list(input_ids)
            for _ in range(max_new_tokens):
                ctx = ids[-self.max_len:]
                t = torch.tensor([ctx], dtype=torch.long, device=device)
                logits = self.forward(t)[0, -1]
                nxt = int(torch.argmax(logits).item())
                if nxt == eos_id:
                    break
                ids.append(nxt)
            return ids

    return _TinyCharLM


# --------------------------------------------------------------------------
# Trainer
# --------------------------------------------------------------------------
class LocalLLMTrainer:
    """Train a small LM locally (MPS/CPU) and export a generator callable.

    Typical flow::

        cfg = TrainConfig(model_source="synthetic", epochs=1, n_samples=32)
        t = LocalLLMTrainer(cfg).train()          # prepare + synth data + fit
        gen = t.export_generator()                # generate(text)->str
        backend = LLMOperatorBackend(gen)         # feed the four operators
    """

    def __init__(self, cfg: TrainConfig) -> None:
        self.cfg = cfg
        self.device = cfg.effective_device()
        self.model = None
        self.tokenizer = None
        self.last_train_device: Optional[str] = None
        self._gen_callable: Optional[Callable[[str], str]] = None

    # -- build --------------------------------------------------------------
    def prepare(self) -> "LocalLLMTrainer":
        if self.cfg.model_source == "synthetic":
            self._prepare_synthetic()
        elif self.cfg.model_source == "hf":
            self._prepare_hf()
        else:
            raise ValueError(f"unknown model_source {self.cfg.model_source!r}")
        return self

    def _prepare_synthetic(self) -> None:
        import torch

        self.tokenizer = _CharTokenizer(_CHAR_VOCAB)
        cls = _build_tiny_lm(len(_CHAR_VOCAB))
        self.model = cls()
        self.model.max_len = self.cfg.max_seq_len
        self.model.to(self.device)
        self.model.train()

    def _prepare_hf(self) -> None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from peft import LoraConfig, get_peft_model
        except ImportError as e:  # pragma: no cover - depends on optional deps
            raise ImportError(
                "HF training requires transformers/peft/accelerate. "
                "Install with: pip install transformers peft accelerate"
            ) from e

        # Name-based param-count guard before downloading (avoid OOM on large checkpoints).
        _KNOWN_PARAM_SIZES = {
            "0.5B": 0.5, "1.5B": 1.5, "3B": 3.0, "7B": 7.0, "8B": 8.0,
            "13B": 13.0, "20B": 20.0, "32B": 32.0, "34B": 34.0, "70B": 70.0,
            "72B": 72.0, "180B": 180.0, "405B": 405.0, "671B": 671.0,
        }
        for suffix, size_b in sorted(_KNOWN_PARAM_SIZES.items(), key=lambda x: -x[1]):
            if suffix in self.cfg.model_id:
                if size_b > self.cfg.max_params_b:
                    raise ValueError(
                        f"model {self.cfg.model_id} is ~{suffix} ({size_b}B), "
                        f"exceeds max_params_b={self.cfg.max_params_b}B"
                    )
                break
        tok = AutoTokenizer.from_pretrained(self.cfg.model_id)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        model = AutoModelForCausalLM.from_pretrained(self.cfg.model_id)
        lora = LoraConfig(
            task_type="CAUSAL_LM",
            r=self.cfg.lora_r,
            lora_alpha=self.cfg.lora_alpha,
            lora_dropout=self.cfg.lora_dropout,
            bias="none",
        )
        model = get_peft_model(model, lora)
        model.to(self.device)
        model.train()
        self.tokenizer = tok
        self.model = model

    # -- dataset ------------------------------------------------------------
    def make_synthetic_dataset(self, teacher_backend=None) -> list[Tuple[str, str]]:
        """Build (prompt_text, program) pairs using an operator backend as teacher.

        Defaults to :class:`TemplateOperatorBackend` so the local model learns to
        imitate the deterministic offline operator outputs -- a self-contained way to
        bootstrap a real generator without an external teacher LLM.
        """

        from .operators import TemplateOperatorBackend, OperatorPrompt

        teacher = teacher_backend or TemplateOperatorBackend()
        ops = ["draft", "improve", "debug", "crossover"]
        pairs: list[Tuple[str, str]] = []
        for i in range(self.cfg.n_samples):
            op = ops[i % 4]
            p = OperatorPrompt(
                operator=op,
                task_description="classify titanic passenger survival",
                target="Survived",
                id_col="PassengerId",
                variant=i % 4,
                feedback="fix shape mismatch between X and y" if op == "debug" else None,
                parent_programs=("parent_program_a", "parent_program_b") if op == "crossover" else (),
            )
            prompt_text = self._operator_prompt_text(p)
            program = teacher.generate(p)
            pairs.append((prompt_text, program))
        return pairs

    @staticmethod
    def _operator_prompt_text(p) -> str:
        text = (
            f"# operator={p.operator}\n"
            f"# task: {p.task_description}\n"
            f"# target={p.target} id_col={p.id_col}\n"
        )
        if p.current_program:
            text += f"# current_program:\n{p.current_program}\n"
        if p.feedback:
            text += f"# feedback:\n{p.feedback}\n"
        for idx, par in enumerate(p.parent_programs, 1):
            text += f"# parent_program_{idx}:\n{par}\n"
        text += "# Return a complete python program writing submission.csv.\n"
        return text

    # -- train --------------------------------------------------------------
    def fit(self, dataset: Sequence[Tuple[str, str]]) -> "LocalLLMTrainer":
        import torch

        if self.model is None:
            self.prepare()
        random.seed(self.cfg.seed)
        torch.manual_seed(self.cfg.seed)

        if self.cfg.model_source == "synthetic":
            self._fit_synthetic(dataset, torch)
        else:  # pragma: no cover - needs optional HF deps + network
            self._fit_hf(dataset, torch)
        self.last_train_device = self.device
        return self

    def _fit_synthetic(self, dataset, torch) -> None:
        from torch.nn import functional as F

        tok = self.tokenizer
        bos, eos = tok.v2i["<bos>"], tok.v2i["<eos>"]
        seq_len = self.cfg.max_seq_len
        # Encode all pairs into truncated token sequences up front.
        seqs = []
        for prompt_text, program in dataset:
            ids = [bos] + tok.encode(prompt_text) + [eos] + tok.encode(program) + [eos]
            ids = ids[:seq_len]
            if len(ids) >= 2:
                seqs.append(ids)

        opt = torch.optim.AdamW(
            [p for p in self.model.parameters() if p.requires_grad], lr=self.cfg.lr
        )
        self.model.train()
        steps = 0
        for epoch in range(self.cfg.epochs):
            random.shuffle(seqs)
            for ids in seqs:
                inp = torch.tensor([ids[:-1]], dtype=torch.long, device=self.device)
                tgt = torch.tensor([ids[1:]], dtype=torch.long, device=self.device)
                logits = self.model.forward(inp)
                loss = F.cross_entropy(logits.view(-1, logits.size(-1)), tgt.view(-1))
                opt.zero_grad()
                loss.backward()
                opt.step()
                steps += 1
                if self.cfg.report_every and steps % self.cfg.report_every == 0:
                    print(f"[synthetic] epoch {epoch+1} step {steps} loss {loss.item():.4f}")
        self.last_train_device = self.device

    def _fit_hf(self, dataset, torch) -> None:  # pragma: no cover - optional deps
        from torch.nn import functional as F

        tok = self.tokenizer
        seq_len = self.cfg.max_seq_len
        opt = torch.optim.AdamW(
            [p for p in self.model.parameters() if p.requires_grad], lr=self.cfg.lr
        )
        enc = tok(
            [p + "\n" + prog for p, prog in dataset],
            truncation=True,
            max_length=seq_len,
            return_tensors="pt",
            padding=True,
        )
        input_ids = enc["input_ids"].to(self.device)
        for epoch in range(self.cfg.epochs):
            for i in range(0, input_ids.size(0), self.cfg.batch_size):
                b = input_ids[i : i + self.cfg.batch_size]
                out = self.model(input_ids=b, labels=b)
                loss = out.loss
                loss.backward()
                opt.step()
                opt.zero_grad()
        self.last_train_device = self.device

    # -- export -------------------------------------------------------------
    def export_generator(self) -> Callable[[str], str]:
        if self.model is None:
            raise RuntimeError("call prepare()/train() before export_generator()")

        if self.cfg.model_source == "synthetic":
            self._gen_callable = self._make_synthetic_generator()
        else:  # pragma: no cover - optional deps
            self._gen_callable = self._make_hf_generator()
        return self._gen_callable

    def _make_synthetic_generator(self) -> Callable[[str], str]:
        import torch

        tok = self.tokenizer
        model = self.model
        bos = tok.v2i["<bos>"]
        eos = tok.v2i["<eos>"]

        def generate(text: str, max_new_tokens: int = 96) -> str:
            torch.manual_seed(self.cfg.seed)
            ids = [bos] + tok.encode(text)
            out_ids = model.generate(ids, max_new_tokens=max_new_tokens, eos_id=eos, device=self.device)
            # strip the prompt prefix + bos, keep generated tail
            gen = out_ids[len(ids):]
            return tok.decode(gen).strip()

        return generate

    def _make_hf_generator(self) -> Callable[[str], str]:  # pragma: no cover - optional deps
        tok = self.tokenizer

        def generate(text: str, max_new_tokens: int = 256) -> str:
            inp = tok(text, return_tensors="pt").to(self.device)
            out = self.model.generate(**inp, max_new_tokens=max_new_tokens)
            return tok.decode(out[0], skip_special_tokens=True)

        return generate

    # -- convenience --------------------------------------------------------
    def train(self) -> "LocalLLMTrainer":
        self.prepare()
        data = self.make_synthetic_dataset()
        self.fit(data)
        return self

    # -- persistence --------------------------------------------------------
    def save(self, output_dir: Optional[str] = None) -> str:
        output_dir = output_dir or self.cfg.output_dir
        os.makedirs(output_dir, exist_ok=True)
        if self.cfg.model_source == "synthetic":
            import torch

            torch.save(
                {
                    "state_dict": self.model.state_dict(),
                    "vocab": _CHAR_VOCAB,
                    "config": self.cfg.__dict__,
                },
                os.path.join(output_dir, "synthetic_lm.pt"),
            )
        else:  # pragma: no cover - optional deps
            self.model.save_pretrained(output_dir)
            self.tokenizer.save_pretrained(output_dir)
        with open(os.path.join(output_dir, "train_config.json"), "w") as f:
            json.dump(self.cfg.__dict__, f, indent=2)
        return output_dir

    def report(self) -> dict:
        """Human-readable summary of the last training run (device, source, sizes)."""

        return {
            "device": self.device,
            "last_train_device": self.last_train_device,
            "model_source": self.cfg.model_source,
            "model_id": self.cfg.model_id,
            "max_params_b": self.cfg.max_params_b,
            "lora_r": self.cfg.lora_r,
            "epochs": self.cfg.epochs,
            "vocab_size": getattr(self.tokenizer, "vocab_size", None),
        }
