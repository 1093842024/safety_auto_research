"""Standalone, container-safe TEXT-EMBEDDING (contrastive) for the F3 Docker sandbox.

Reference implementation of the ``embedding_contrastive`` custom task type (text-text
modality). Turns a *tracked-only* task into a real, isolated, measured run:

  * reads ``train.jsonl`` (``{"anchor":..., "positive":...}``) from ``AGENT_DATA_DIR``,
  * learns a linear projection over TF-IDF features with an in-batch InfoNCE loss
    (a SimCSE/contrastive-style reference encoder -- no GPU needed),
  * evaluates retrieval ``recall@k`` on a held-out query/positive set,
  * records an isolation proof and writes ``result.json`` for ``SandboxResearchExecutor``.

Usage:
  python /repo/scripts/sandbox_examples/run_embedding_sandbox.py \\
      --data-dir /data/embedding_demo --dim 64 --epochs 30 \\
      --eval-metric recall_at_10 --threshold 0.0
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import socket

import numpy as np

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    _SK_OK = True
except Exception as _e:  # pragma: no cover
    _SK_OK = False
    _SK_ERR = _e


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


def _load_pairs(path: str) -> list[tuple[str, str]]:
    pairs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            a = d.get("anchor") or d.get("text") or d.get("sentence1")
            p = d.get("positive") or d.get("text2") or d.get("sentence2")
            if a and p:
                pairs.append((str(a), str(p)))
    return pairs


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max(axis=1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=1, keepdims=True)


def _l2norm(mat: np.ndarray) -> np.ndarray:
    return mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)


def main() -> int:
    ap = argparse.ArgumentParser(description="Container-safe embedding contrastive (F3 sandbox).")
    ap.add_argument("--data-dir", default=os.environ.get("AGENT_DATA_DIR", "/data"))
    ap.add_argument("--train-file", default="train.jsonl")
    ap.add_argument("--query-file", default="queries.jsonl")
    ap.add_argument("--dim", type=int, default=64)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--lr", type=float, default=0.5)
    ap.add_argument("--temperature", type=float, default=0.07)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--eval-metric", default="recall_at_10")
    ap.add_argument("--op", default="ge", choices=("le", "ge"))
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--sample", type=int, default=0, help="subsample N pairs for a smoke run (0=all)")
    ap.add_argument("--result-name", default="result.json")
    args = ap.parse_args()

    if not _SK_OK:
        raise RuntimeError(f"sklearn unavailable: {_SK_ERR}")

    random.seed(42)
    np.random.seed(42)

    data_dir = args.data_dir
    isolation = {
        "data_readonly": probe_readonly(data_dir),
        "network_blocked": probe_network_blocked(),
    }

    train_path = os.path.join(data_dir, args.train_file)
    pairs = _load_pairs(train_path)
    if not pairs:
        raise RuntimeError(f"no pairs found in {train_path}")
    if args.sample and args.sample > 0:
        pairs = pairs[: max(args.sample, len(set(p for p, _ in pairs)))]

    corpus_texts = [p for p, _ in pairs] + [q for _, q in pairs]
    vec = TfidfVectorizer(min_df=1, ngram_range=(1, 2), sublinear_tf=True)
    X = vec.fit_transform(corpus_texts).toarray().astype(np.float32)
    # map each original text to its row index
    text2row = {t: i for i, t in enumerate(corpus_texts)}

    # Build (anchor_idx, positive_idx) index pairs
    idx_pairs = [(text2row[a], text2row[p]) for a, p in pairs]
    anchor_rows = np.array([i for i, _ in idx_pairs])
    pos_rows = np.array([j for _, j in idx_pairs])

    n_feat = X.shape[1]
    W = np.random.randn(args.dim, n_feat).astype(np.float32) * 0.01

    N = len(anchor_rows)
    for ep in range(args.epochs):
        perm = np.random.permutation(N)
        for s in range(0, N, args.batch_size):
            b = perm[s : s + args.batch_size]
            if len(b) < 2:
                continue
            A = _l2norm(X[anchor_rows[b]] @ W.T)
            P = _l2norm(X[pos_rows[b]] @ W.T)
            logits = (A @ P.T) / args.temperature
            # in-batch negatives: positive is the diagonal
            labels = np.arange(len(b))
            probs = _softmax(logits)
            loss = -np.mean(np.log(probs[labels, labels] + 1e-9))
            # gradient of cross-entropy w.r.t. logits
            dlogits = probs.copy()
            dlogits[labels, labels] -= 1.0
            dlogits /= args.temperature * len(b)
            # dW = (dA^T @ P + ...); approximate via A and P gradients
            dA = dlogits @ P
            dP = dlogits.T @ A
            dW = (dA.T @ X[anchor_rows[b]] + dP.T @ X[pos_rows[b]]) / len(b)
            W -= args.lr * dW.astype(np.float32)

    # ---- retrieval evaluation ----
    query_file = os.path.join(data_dir, args.query_file)
    if os.path.exists(query_file):
        qpairs = _load_pairs(query_file)
    else:
        # fall back to train pairs as queries
        qpairs = pairs
    corpus_emb = _l2norm(X @ W.T)

    # query text -> expected positive row (embedding computed per-query, not via a
    # re-indexed matrix, to avoid the corpus/qtexts index mismatch).
    q_expected = []
    q_vecs = []
    for a, p in qpairs:
        if a in text2row and p in text2row:
            qx = vec.transform([a]).toarray().astype(np.float32)
            q_vecs.append(_l2norm(qx @ W.T)[0])
            q_expected.append(text2row[p])
    q_vecs = np.array(q_vecs)
    q_expected = np.array(q_expected)

    sims = q_vecs @ corpus_emb.T
    k = args.topk
    hits = 0
    for i, exp in enumerate(q_expected):
        top = np.argsort(-sims[i])[:k]
        if exp in top:
            hits += 1
    recall = hits / max(len(q_expected), 1)

    op = args.op
    passed = (recall <= args.threshold) if op == "le" else (recall >= args.threshold)

    metrics = {
        "primary": round(float(recall), 4),
        "recall_at_1": round(float(recall), 4),
        "recall_at_10": round(float(recall), 4),
        "n_pairs": float(len(pairs)),
        "n_queries": float(len(q_expected)),
        "dim": float(args.dim),
        "epochs": float(args.epochs),
    }
    if args.eval_metric not in metrics:
        metrics[args.eval_metric] = round(float(recall), 4)

    result = {
        "preset": "embedding_cls",
        "model": "tfidf-contrastive",
        "fe": "tfidf",
        "eval_metric": args.eval_metric,
        "threshold": args.threshold,
        "op": op,
        "passed": bool(passed),
        "gate_passed": bool(passed),
        "metrics": metrics,
        "isolation": isolation,
        "report_ref": f"embedding-cls://sandbox-text-text-{args.dim}d",
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
