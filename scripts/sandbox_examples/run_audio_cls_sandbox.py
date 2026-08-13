"""Standalone, container-safe AUDIO classification for the F3 Docker sandbox.

The non-kaggle counterpart of ``run_text_cls_sandbox.py`` for the
``audio_classification`` custom task type. It turns a *tracked-only* task into a
real, isolated, measured run:

  * reads a manifest CSV (``filepath,label``) from ``AGENT_DATA_DIR`` (read-only),
  * extracts log-Mel / MFCC features via librosa,
  * trains a small 2-D CNN over the spectrogram,
  * evaluates accuracy,
  * records an isolation proof and writes ``result.json`` for ``SandboxResearchExecutor``.

Usage:
  python /repo/scripts/sandbox_examples/run_audio_cls_sandbox.py \\
      --manifest /data/audio_cls_demo/manifest.csv --data-dir /data/audio_cls_demo \\
      --feature logmel --epochs 8 --eval-metric accuracy --threshold 0.5
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import socket

import numpy as np

try:
    import librosa  # noqa: F401
    _OK = True
except Exception as _e:  # pragma: no cover
    _OK = False
    _ERR = _e

# torch is always present in the sandbox image; import it at module load so the
# model class below (which references torch.nn) can be defined unconditionally.
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, random_split


def probe_readonly(data_dir: str) -> bool:
    probe = os.path.join(data_dir, ".sandbox_write_probe")
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        try:
            os.remove(probe)
        except OSError:
            pass
        return False
    except OSError:
        return True


def probe_network_blocked() -> bool:
    for host in ("8.8.8.8", "registry-1.docker.io", "pypi.org"):
        try:
            with socket.create_connection((host, 443), timeout=2):
                return False
        except OSError:
            continue
    return True


N_MELS = 40
CLIP_FRAMES = 80  # fixed time dimension after cropping/padding


class _MelCNN(nn.Module):
    def __init__(self, num_classes: int):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(1, 16, 3, padding=1),
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, 3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.AdaptiveAvgPool2d((8, 8)),
        )
        self.head = nn.Sequential(
            nn.Flatten(), nn.Linear(32 * 8 * 8, 64), nn.ReLU(), nn.Linear(64, num_classes)
        )

    def forward(self, x):
        return self.head(self.conv(x))


def _extract(path: str, sr: int, feature: str) -> np.ndarray:
    y, _ = librosa.load(path, sr=sr, duration=5.0)
    if len(y) == 0:
        y = np.zeros(sr, dtype=np.float32)
    if feature == "mfcc":
        feat = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=N_MELS, n_mels=N_MELS)
    else:  # logmel
        S = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=N_MELS)
        feat = librosa.power_to_db(S, ref=np.max)
    # crop / pad to CLIP_FRAMES along time
    if feat.shape[1] > CLIP_FRAMES:
        feat = feat[:, :CLIP_FRAMES]
    elif feat.shape[1] < CLIP_FRAMES:
        pad = CLIP_FRAMES - feat.shape[1]
        feat = np.pad(feat, ((0, 0), (0, pad)), mode="constant")
    return feat.astype(np.float32)


class _AudioDS(Dataset):
    def __init__(self, paths, labels, sr, feature):
        self.paths, self.labels, self.sr, self.feature = paths, labels, sr, feature

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, i):
        feat = _extract(self.paths[i], self.sr, self.feature)
        return torch.tensor(feat).unsqueeze(0), self.labels[i]


def main() -> int:
    ap = argparse.ArgumentParser(description="Container-safe audio classification (F3 sandbox).")
    ap.add_argument("--manifest", required=True, help="CSV with columns filepath,label")
    ap.add_argument("--data-dir", default=os.environ.get("AGENT_DATA_DIR", "/data"))
    ap.add_argument("--sample-rate", type=int, default=16000)
    ap.add_argument("--feature", default="logmel", choices=("logmel", "mfcc"))
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--eval-metric", default="accuracy")
    ap.add_argument("--op", default="ge", choices=("le", "ge"))
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--sample", type=int, default=0, help="subsample N rows for a smoke run (0=all)")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    if not _OK:
        raise RuntimeError(f"librosa/torch unavailable: {_ERR}")

    torch.manual_seed(42)
    random.seed(42)
    np.random.seed(42)
    device = torch.device("cpu")

    isolation = {
        "data_readonly": probe_readonly(args.data_dir),
        "network_blocked": probe_network_blocked(),
    }

    paths, labels = [], []
    with open(args.manifest, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            p = row.get("filepath") or row.get("path") or ""
            lbl = row.get("label")
            if not p or lbl is None:
                continue
            if not os.path.isabs(p):
                p = os.path.join(args.data_dir, p)
            paths.append(p)
            labels.append(lbl)

    if not paths:
        raise RuntimeError(f"manifest {args.manifest} yielded 0 usable rows")

    classes = sorted(set(labels))
    label2id = {c: i for i, c in enumerate(classes)}
    y = [label2id[c] for c in labels]

    if args.sample and args.sample > 0:
        idx = list(range(len(paths)))
        random.shuffle(idx)
        idx = idx[: max(args.sample, len(classes))]
        paths = [paths[i] for i in idx]
        y = [y[i] for i in idx]

    n_classes = len(classes)
    ds = _AudioDS(paths, y, args.sample_rate, args.feature)
    n_val = max(1, int(len(ds) * 0.2))
    n_tr = len(ds) - n_val
    train_ds, val_ds = random_split(ds, [n_tr, n_val], generator=torch.Generator().manual_seed(42))
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False)

    model = _MelCNN(n_classes).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    crit = nn.CrossEntropyLoss()

    for _ in range(args.epochs):
        model.train()
        for x, yb in train_loader:
            x, yb = x.to(device), yb.to(device)
            opt.zero_grad()
            loss = crit(model(x), yb)
            loss.backward()
            opt.step()

    model.eval()
    correct = total = 0
    with torch.no_grad():
        for x, yb in val_loader:
            x, yb = x.to(device), yb.to(device)
            pred = model(x).argmax(1)
            correct += int((pred == yb).sum())
            total += int(yb.numel())
    acc = correct / max(total, 1)

    op = args.op
    passed = (acc <= args.threshold) if op == "le" else (acc >= args.threshold)

    metrics = {
        "primary": round(acc, 4),
        "accuracy": round(acc, 4),
        "n_classes": float(n_classes),
        "n_train": float(len(train_ds)),
        "n_test": float(len(val_ds)),
        "feature": args.feature,
        "epochs": float(args.epochs),
    }
    if args.eval_metric not in metrics:
        metrics[args.eval_metric] = round(acc, 4)

    result = {
        "preset": "audio_cls",
        "model": "melcnn",
        "fe": args.feature,
        "eval_metric": args.eval_metric,
        "threshold": args.threshold,
        "op": op,
        "passed": bool(passed),
        "gate_passed": bool(passed),
        "metrics": metrics,
        "isolation": isolation,
        "report_ref": f"audio-cls://sandbox-{args.feature}-{n_classes}cls",
    }

    scratch = os.environ.get("AGENT_SCRATCH_DIR", os.getcwd())
    os.makedirs(scratch, exist_ok=True)
    out_path = os.path.join(scratch, args.result_name or "result.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
