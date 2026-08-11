"""Generate small, self-contained sample datasets for the non-tabular benchmark tasks.

These datasets are tiny (a few MB at most) and live entirely under
``benchmark_tasks/sample_data/`` so the platform can actually *run* and *optimize*
``text_classification`` / ``image_classification`` / ``audio_classification`` /
``embedding_contrastive`` tasks without any external download. They are reference
fixtures, not research-grade corpora.

Run:  python benchmark_tasks/sample_data/generate_sample_data.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import struct
import wave

import numpy as np
from PIL import Image

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _write_wav(path: str, samples: np.ndarray, sr: int = 16000) -> None:
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def gen_text(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    rng = np.random.default_rng(42)
    topics = {
        "sports": ["比赛", "球队", "进球", "冠军", "联赛", "运动员", "胜利", "比分"],
        "tech": ["模型", "代码", "算法", "芯片", "数据", "训练", "推理", "参数"],
        "food": ["美食", "餐厅", "口味", "烹饪", "食材", "甜点", "早餐", "辣味"],
    }
    rows = []
    for label, words in topics.items():
        for _ in range(120):
            a = f"今天{' '.join(rng.choice(words, 3, replace=False))}的讨论很热烈。"
            b = f"我们聊了关于{' '.join(rng.choice(words, 3, replace=False))}的话题。"
            rows.append((a, label))
            rows.append((b, label))
    rng.shuffle(rows)
    with open(os.path.join(root, "train.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["text", "label"])
        for text, label in rows:
            w.writerow([text, label])


def gen_image(root: str, img_size: int = 32) -> None:
    rng = np.random.default_rng(7)
    classes = ["red", "green", "blue", "yellow", "cyan"]
    # Flat ImageFolder layout: <root>/<class>/*.png (the runner splits train/val
    # internally via random_split, so no nested train/val dirs are needed).
    for ci, cls in enumerate(classes):
        d = os.path.join(root, cls)
        os.makedirs(d, exist_ok=True)
        base = (np.array([(255, 60, 60), (60, 220, 60), (60, 60, 255),
                         (255, 230, 40), (40, 230, 230)][ci])).astype(np.float32)
        for i in range(75):
            arr = np.zeros((img_size, img_size, 3), dtype=np.float32)
            # a colored shape on a neutral background
            cy, cx = rng.integers(8, img_size - 8, 2)
            r = rng.integers(5, 10)
            yy, xx = np.ogrid[:img_size, :img_size]
            mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
            shade = rng.uniform(0.6, 1.0)
            arr[mask] = base * shade
            img = Image.fromarray(arr.astype(np.uint8))
            img.save(os.path.join(d, f"{cls}_{i:03d}.png"))


def gen_audio(root: str, sr: int = 16000) -> None:
    os.makedirs(root, exist_ok=True)
    rng = np.random.default_rng(11)
    classes = ["tone_220", "tone_440", "tone_660", "tone_880"]
    freqs = [220, 440, 660, 880]
    manifest = []
    for ci, (cls, f) in enumerate(zip(classes, freqs)):
        d = os.path.join(root, cls)
        os.makedirs(d, exist_ok=True)
        for i in range(40):
            t = np.linspace(0, 2.0, int(2.0 * sr), dtype=np.float32)
            sig = 0.6 * np.sin(2 * math.pi * f * t)
            sig += 0.05 * rng.standard_normal(t.shape).astype(np.float32)  # light noise
            # amplitude envelope
            env = np.ones_like(sig)
            env[:int(0.05 * sr)] = np.linspace(0, 1, int(0.05 * sr))
            env[-int(0.05 * sr):] = np.linspace(1, 0, int(0.05 * sr))
            sig *= env
            fname = f"{cls}_{i:03d}.wav"
            _write_wav(os.path.join(d, fname), sig, sr)
            manifest.append((f"{cls}/{fname}", cls))
    with open(os.path.join(root, "manifest.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["filepath", "label"])
        for p, lbl in manifest:
            w.writerow([p, lbl])


def gen_embedding(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    topics = {
        "weather": ["今天天气晴朗适合外出", "外面阳光明媚适宜散步", "明天可能会下雨记得带伞", "气象预报说午后转阴"],
        "sports": ["主队在加时赛绝杀对手", "足球比赛以一球险胜", "篮球决赛进入点球大战", "运动员打破了世界纪录"],
        "tech": ["新模型在基准测试上领先", "这段代码存在内存泄漏", "算法复杂度可以优化到线性", "芯片制程进入纳米时代"],
    }
    train, queries = [], []
    for topic, sents in topics.items():
        for i in range(len(sents)):
            for j in range(len(sents)):
                if i != j:
                    train.append({"anchor": sents[i], "positive": sents[j]})
        for s in sents:
            queries.append({"anchor": s, "positive": s})
    with open(os.path.join(root, "train.jsonl"), "w", encoding="utf-8") as fh:
        for r in train:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(root, "queries.jsonl"), "w", encoding="utf-8") as fh:
        for r in queries:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    gen_text(os.path.join(_ROOT, "text_cls_demo"))
    gen_image(os.path.join(_ROOT, "image_cls_demo"))
    gen_audio(os.path.join(_ROOT, "audio_cls_demo"))
    gen_embedding(os.path.join(_ROOT, "embedding_demo"))
    print("sample data written under:", _ROOT)


if __name__ == "__main__":
    main()
