"""Standalone, container-safe IMAGE classification for the F3 Docker sandbox.

The non-kaggle counterpart of ``run_text_cls_sandbox.py`` for the
``image_classification`` custom task type. It turns a previously *tracked-only*
task into a real, isolated, measured run:

  * reads an ``ImageFolder`` (``<data-dir>/<class>/*.png``; optional ``val/``) from
    ``AGENT_DATA_DIR`` (read-only mount),
  * trains a small torchvision CNN (``--arch tiny_cnn`` from scratch, or
    ``--arch resnet18`` transfer-learning),
  * evaluates top-1 accuracy,
  * records an isolation proof (data read-only, network blocked),
  * writes ``result.json`` that ``SandboxResearchExecutor`` turns into a real
    ``EvalCompletedEvent``.

Usage:
  python /repo/scripts/sandbox_examples/run_image_cls_sandbox.py \\
      --data-dir /data/image_cls_demo --arch tiny_cnn --epochs 6 \\
      --eval-metric accuracy --threshold 0.5 --result-name result.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.utils.data import DataLoader, Subset, random_split
    from torchvision import transforms
    from torchvision.datasets import ImageFolder
    _TORCH_OK = True
except Exception as _e:  # pragma: no cover - environment guard
    _TORCH_OK = False
    _TORCH_ERR = _e


# --------------------------------------------------------------------------- #
# Isolation probes (same spirit as run_text_cls_sandbox.py)                    #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Model factory                                                                #
# --------------------------------------------------------------------------- #
class _TinyCNN(nn.Module):
    """2-conv + 2-fc CNN, sized to ``in_channels`` at a fixed ``img_size``."""

    def __init__(self, num_classes: int, img_size: int = 32, channels: int = 3):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(channels, 16, 3, padding=1),
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, 3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d(2),
        )
        feat = 32 * (img_size // 4) * (img_size // 4)
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Linear(feat, 64),
            nn.ReLU(),
            nn.Linear(64, num_classes),
        )

    def forward(self, x):
        return self.head(self.conv(x))


def _build_model(arch: str, num_classes: int, img_size: int, channels: int, pretrained: bool):
    if arch == "tiny_cnn":
        return _TinyCNN(num_classes, img_size, channels)
    if arch in ("resnet18", "resnet50"):
        import torchvision.models as models

        net = getattr(models, arch)(weights="DEFAULT" if pretrained else None)
        in_f = net.fc.in_features
        net.fc = nn.Linear(in_f, num_classes)
        return net
    # default: tiny cnn
    return _TinyCNN(num_classes, img_size, channels)


def _scorer(metric: str):
    # only top-1 accuracy supported for image; extend here if needed
    return "accuracy"


# --------------------------------------------------------------------------- #
# Train / eval                                                                 #
# --------------------------------------------------------------------------- #
def _evaluate(model, loader, device):
    model.eval()
    correct = total = 0
    with torch.no_grad():
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            pred = model(x).argmax(1)
            correct += int((pred == y).sum())
            total += int(y.numel())
    return correct / max(total, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Container-safe image classification (F3 sandbox).")
    ap.add_argument("--data-dir", default=os.environ.get("AGENT_DATA_DIR", "/data"),
                    help="ImageFolder root (read-only mount): <root>/<class>/*.png")
    ap.add_argument("--image-size", type=int, default=32)
    ap.add_argument("--arch", default="tiny_cnn", choices=("tiny_cnn", "resnet18", "resnet50"))
    ap.add_argument("--pretrained", action="store_true", default=True,
                    help="use ImageNet weights for resnet (default on)")
    ap.add_argument("--no-pretrained", dest="pretrained", action="store_false")
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--aug", default="none", choices=("none", "basic"))
    ap.add_argument("--eval-metric", default="accuracy")
    ap.add_argument("--op", default="ge", choices=("le", "ge"))
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--test-size", type=float, default=0.2)
    ap.add_argument("--sample", type=int, default=0,
                    help="subsample N images per class for a quick smoke run (0 = all)")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    if not _TORCH_OK:
        raise RuntimeError(f"torch/torchvision unavailable: {_TORCH_ERR}")

    torch.manual_seed(42)
    random.seed(42)
    np.random.seed(42)
    device = torch.device("cpu")

    isolation = {
        "data_readonly": probe_readonly(args.data_dir),
        "network_blocked": probe_network_blocked(),
    }

    tf_train = transforms.Compose([
        transforms.Resize((args.image_size, args.image_size)),
        transforms.RandomHorizontalFlip() if args.aug == "basic" else transforms.Lambda(lambda im: im),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    tf_val = transforms.Compose([
        transforms.Resize((args.image_size, args.image_size)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    full = ImageFolder(args.data_dir, transform=tf_train)
    # determine channel count from first image
    try:
        channels = full[0][0].shape[0]
    except Exception:
        channels = 3
    # Re-create with correct channel normalization-less transform (keep simple RGB path).
    full = ImageFolder(args.data_dir, transform=tf_train)
    n_classes = len(full.classes)

    # optional per-class subsample
    if args.sample and args.sample > 0:
        idx_by_class = {}
        for idx, (_, y) in enumerate(full.samples):
            idx_by_class.setdefault(y, []).append(idx)
        keep = []
        for y, idxs in idx_by_class.items():
            keep += idxs[: max(args.sample, 1)]
        full = Subset(full, keep)

    n_val = max(1, int(len(full) * args.test_size))
    n_tr = len(full) - n_val
    train_ds, val_ds = random_split(full, [n_tr, n_val], generator=torch.Generator().manual_seed(42))
    # val uses val transform
    val_ds.dataset.transform = tf_val  # type: ignore[attr-defined]
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False)

    model = _build_model(args.arch, n_classes, args.image_size, channels, args.pretrained).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    crit = nn.CrossEntropyLoss()

    for _ in range(args.epochs):
        model.train()
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            loss = crit(model(x), y)
            loss.backward()
            opt.step()

    acc = _evaluate(model, val_loader, device)
    op = args.op
    passed = (acc <= args.threshold) if op == "le" else (acc >= args.threshold)

    metrics = {
        "primary": round(acc, 4),
        "accuracy": round(acc, 4),
        "top1_accuracy": round(acc, 4),
        "n_classes": float(n_classes),
        "n_train": float(len(train_ds)),
        "n_test": float(len(val_ds)),
        "arch": args.arch,
        "epochs": float(args.epochs),
    }
    if args.eval_metric not in metrics:
        metrics[args.eval_metric] = round(acc, 4)

    result = {
        "preset": "image_cls",
        "model": args.arch,
        "fe": "cnn",
        "eval_metric": args.eval_metric,
        "threshold": args.threshold,
        "op": op,
        "passed": bool(passed),
        "gate_passed": bool(passed),
        "metrics": metrics,
        "isolation": isolation,
        "report_ref": f"image-cls://sandbox-{args.arch}-{n_classes}cls",
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
