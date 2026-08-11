"""Source a REAL text-classification dataset for the F3 sandbox demo.

F2 data bottleneck: non-kaggle tasks depend on datasets that aren't on the host.
This script downloads a real, well-known text benchmark on the *host* (network is
allowed here) and writes a labelled CSV that the sandbox container then mounts
read-only. The container itself runs with ``--network none`` so it never needs to
reach the internet -- the data is supplied, not fetched.

Default: the SMS Spam Collection (UCI / HuggingFace ``fancyzhx/sms-spam-collection``)
-- a classic real binary text-classification benchmark (~5.5k SMS, ham vs spam).
Pass ``--help`` for options.

Output: data/benchmark/text_cls_demo/train.csv  (columns: text, label)
"""

from __future__ import annotations

import argparse
import os

import pandas as pd

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEFAULT_CATEGORIES = [
    "sci.space",
    "rec.sport.baseball",
    "talk.politics.mideast",
    "comp.graphics",
]


def fetch_20newsgroups(categories: list[str]) -> pd.DataFrame:
    from sklearn.datasets import fetch_20newsgroups

    data = fetch_20newsgroups(
        subset="train", categories=categories, shuffle=True, random_state=42
    )
    return pd.DataFrame(
        {"text": list(data.data), "label": [data.target_names[i] for i in data.target]}
    )


def fetch_sms_spam() -> pd.DataFrame:
    from datasets import load_dataset

    ds = load_dataset("fancyzhx/sms-spam-collection", split="train")
    df = ds.to_pandas()
    # Normalize the two common column layouts to (text, label).
    if "text" in df.columns and "label" in df.columns:
        out = df[["text", "label"]].copy()
    elif "v2" in df.columns and "v1" in df.columns:
        out = df.rename(columns={"v2": "text", "v1": "label"})[["text", "label"]].copy()
    else:
        raise ValueError(f"unexpected SMS-Spam columns: {list(df.columns)}")
    out["text"] = out["text"].astype(str)
    out["label"] = out["label"].astype(str).str.lower()
    return out


def fetch_stanford_sst2() -> pd.DataFrame:
    """Stanford Sentiment Treebank v2 (SST2) -- canonical real binary sentiment
    benchmark (~67k train). Downloaded directly as parquet from the HuggingFace
    Hub (the ``datasets`` GLUE loader is broken on new hf_hub versions)."""
    import subprocess
    import tempfile

    url = "https://huggingface.co/datasets/stanfordnlp/sst2/resolve/main/data/train-00000-of-00001.parquet"
    tmp = tempfile.mktemp(suffix=".parquet")
    try:
        subprocess.run(["curl", "-sSL", "-m", "120", "-o", tmp, url], check=True)
        df = pd.read_parquet(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = pd.DataFrame(
        {
            "text": df["sentence"].astype(str).tolist(),
            "label": ["positive" if int(l) == 1 else "negative" for l in df["label"]],
        }
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Download a real text-cls dataset for the sandbox.")
    ap.add_argument(
        "--source", choices=("stanford_sst2", "sms_spam", "20newsgroups"), default="stanford_sst2",
        help="which real dataset to download (default: stanford_sst2)",
    )
    ap.add_argument("--categories", nargs="+", default=_DEFAULT_CATEGORIES,
                    help="20newsgroups category names (only used with --source 20newsgroups)")
    ap.add_argument("--out", default=os.path.join(_PKG_ROOT, "data", "benchmark", "text_cls_demo", "train.csv"))
    ap.add_argument("--limit", type=int, default=0, help="keep only the first N rows (0 = all)")
    args = ap.parse_args()

    if args.source == "20newsgroups":
        print(f"[dataset] fetching 20 Newsgroups subset: {args.categories}")
        df = fetch_20newsgroups(args.categories)
    elif args.source == "sms_spam":
        print("[dataset] fetching SMS Spam Collection (HuggingFace fancyzhx/sms-spam-collection)")
        df = fetch_sms_spam()
    else:
        print("[dataset] fetching Stanford SST2 (HuggingFace stanfordnlp/sst2)")
        df = fetch_stanford_sst2()

    if args.limit and args.limit > 0:
        df = df.head(args.limit)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"[dataset] wrote {len(df)} rows -> {args.out}")
    print(df["label"].value_counts().to_string())


if __name__ == "__main__":
    main()
