import os

import numpy as np

from model import compute_metrics, count_trainable_params, predict_labels, save_model, train_router


MIN_ACCURACY = 0.64
MIN_UNSAFE_RECALL = 0.66
MIN_SAFE_RECALL = 0.57


def load_split(path):
    with np.load(path) as data:
        return data["X"], data["y"]


def passes_gates(metrics):
    return (
        metrics.accuracy >= MIN_ACCURACY
        and metrics.unsafe_recall >= MIN_UNSAFE_RECALL
        and metrics.safe_recall >= MIN_SAFE_RECALL
    )


def main():
    X_train, y_train = load_split("data/train.npz")
    X_val, y_val = load_split("data/val.npz")

    candidates = []
    for hidden_dim in (8, 12, 16):
        for positive_weight in (1.15, 1.3, 1.5, 1.8, 2.0, 2.5, 3.0):
            router = train_router(
                X_train,
                y_train,
                X_val,
                y_val,
                hidden_dim=hidden_dim,
                epochs=240,
                positive_weight=positive_weight,
            )
            metrics = compute_metrics(y_val, predict_labels(router, X_val))
            params = count_trainable_params(router)
            print(
                f"hidden_dim={hidden_dim} positive_weight={positive_weight} "
                f"params={params} accuracy={metrics.accuracy:.6f} "
                f"unsafe_recall={metrics.unsafe_recall:.6f} "
                f"safe_recall={metrics.safe_recall:.6f}"
            )
            if passes_gates(metrics):
                candidates.append((params, -positive_weight, router))

    if not candidates:
        raise RuntimeError("no hidden dimension passed validation gates")

    _, _, chosen = min(candidates, key=lambda item: item[:2])
    hidden_dim = chosen["W1"].shape[1]
    os.makedirs("model_artifacts", exist_ok=True)
    save_model(chosen, "model_artifacts/router_model.npz")
    print(f"selected hidden_dim={hidden_dim}")


if __name__ == "__main__":
    main()
