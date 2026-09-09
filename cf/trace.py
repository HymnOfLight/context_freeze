"""Request-sequence generators: sigma = (r_1, ..., r_T) over the Top-k app set.

Generators return a list of app indices / names. Options mirror the experiment plan:
- zipf popularity + "stickiness" (probability of returning to the previous app)
- Markov chain with a random sparse transition matrix
- distribution drift: popularity permuted at `drift_at` (habit change test)
- replay of a recorded trace (JSONL from the runner or plain text)
"""
from __future__ import annotations

import json
import random
from typing import Sequence


def zipf_weights(n: int, s: float) -> list[float]:
    w = [1.0 / (i + 1) ** s for i in range(n)]
    tot = sum(w)
    return [x / tot for x in w]


def gen_zipf(apps: Sequence[str], T: int, s: float = 1.0, stickiness: float = 0.3,
             seed: int = 0, drift_at: int | None = None, no_repeat: bool = True) -> list[str]:
    rng = random.Random(seed)
    order = list(range(len(apps)))
    rng.shuffle(order)
    w = zipf_weights(len(apps), s)
    seq: list[int] = []
    prev2 = None
    for t in range(T):
        if drift_at is not None and t == drift_at:
            rng.shuffle(order)
        for _ in range(100):
            if prev2 is not None and seq and rng.random() < stickiness:
                cand = prev2  # A -> B -> A pattern
            else:
                cand = rng.choices(order, weights=w)[0]
            if not (no_repeat and seq and cand == seq[-1]):
                break
        prev2 = seq[-1] if seq else None
        seq.append(cand)
    return [apps[i] for i in seq]


def gen_markov(apps: Sequence[str], T: int, seed: int = 0, sparsity: float = 0.4,
               drift_at: int | None = None) -> list[str]:
    rng = random.Random(seed)
    n = len(apps)

    def random_matrix() -> list[list[float]]:
        M = []
        for i in range(n):
            row = [rng.random() if (rng.random() > sparsity and j != i) else 0.0 for j in range(n)]
            if sum(row) == 0:
                row[(i + 1) % n] = 1.0
            s = sum(row)
            M.append([x / s for x in row])
        return M

    M = random_matrix()
    cur = rng.randrange(n)
    seq = [cur]
    for t in range(1, T):
        if drift_at is not None and t == drift_at:
            M = random_matrix()
        cur = rng.choices(range(n), weights=M[cur])[0]
        seq.append(cur)
    return [apps[i] for i in seq]


def load_replay(path: str) -> list[str]:
    seq = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                rec = json.loads(line)
                if rec.get("type", "step") == "step" and "req" in rec:
                    seq.append(rec["req"])
            else:
                seq.append(line.split()[0])
    return seq


def make_trace(apps: Sequence[str], cfg: dict) -> list[str]:
    kind = cfg.get("kind", "zipf")
    T = int(cfg.get("T", 60))
    if kind == "zipf":
        return gen_zipf(apps, T, s=cfg.get("s", 1.0), stickiness=cfg.get("stickiness", 0.3),
                        seed=cfg.get("seed", 0), drift_at=cfg.get("drift_at"))
    if kind == "markov":
        return gen_markov(apps, T, seed=cfg.get("seed", 0), sparsity=cfg.get("sparsity", 0.4),
                          drift_at=cfg.get("drift_at"))
    if kind == "replay":
        seq = load_replay(cfg["path"])
        return [a for a in seq if a in set(apps)][:T] if T else seq
    if kind == "round_robin":
        return [apps[t % len(apps)] for t in range(T)]
    raise ValueError(f"unknown trace kind {kind}")
