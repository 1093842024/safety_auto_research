import json

import numpy as np


W_high = np.load(
    "benchmark/datasets/jnmf_visualization/fit_result_conscientiousness_W_high.npy"
)
W_low = np.load(
    "benchmark/datasets/jnmf_visualization/fit_result_conscientiousness_W_low.npy"
)
H_high = np.load(
    "benchmark/datasets/jnmf_visualization/fit_result_conscientiousness_H_high.npy"
)
H_low = np.load(
    "benchmark/datasets/jnmf_visualization/fit_result_conscientiousness_H_low.npy"
)

importance_factors = {
    "common error": float(1 - np.inner(W_high[:, 0], W_low[:, 0])),
    "common importance": float(
        (H_high[:, 0].sum() + H_low[:, 0].sum())
        / (H_high.sum() + H_low.sum())
    ),
    "high importance": float(H_high[:, 1].sum() / H_high.sum()),
    "low importance": float(H_low[:, 1].sum() / H_low.sum()),
    "distinct error": float(np.inner(W_high[:, 1], W_low[:, 1])),
    "distinct importance": float(
        (H_high[:, 1].sum() + H_low[:, 1].sum())
        / (H_high.sum() + H_low.sum())
    ),
}

with open("pred_results/jnmf_h_importances.json", "w") as output_file:
    json.dump(importance_factors, output_file)
