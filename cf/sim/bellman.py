"""Offline Bellman potential  V_t(S)  over resident subsets (bitmask states).

    V_{T+1}(S) = G_T(S)
    V_t(S)     = min_{S' feasible for r_t} [ lambda_z * moved(S, S') + lambda_m * M(S')
                                            + lambda_L * phi(L(S, r_t)) + V_{t+1}(S') ]

With k <= 12 apps the 2^k x 2^k transition matrix is materialised once
(moved(S, S') = A[S xor S']) and every step is one vectorised min over S'.
Returns OPT(sigma) = V_1(S_0) and the optimal action sequence.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .model import MB, Lambdas, RunResult, SimApp, step_cost, terminal_cost


def _subset_sums(values: np.ndarray) -> np.ndarray:
    k = len(values)
    out = np.zeros(1 << k)
    for i in range(k):
        bit = 1 << i
        idx = np.arange(1 << k)
        out[(idx & bit) != 0] += values[i]
    return out


def solve_opt(trace: Sequence[str], apps: Sequence[SimApp], lam: Lambdas, B: float,
              initial_resident: Optional[set[str]] = None) -> tuple[RunResult, np.ndarray]:
    k = len(apps)
    if k > 14:
        raise ValueError("Bellman DP over subsets is limited to k <= 14")
    names = [a.name for a in apps]
    idx = {n: i for i, n in enumerate(names)}
    a_vec = np.array([a.a for a in apps]) / MB
    m_vec = np.array([a.m for a in apps]) / MB
    rho_a = np.array([a.rho * a.a for a in apps]) / MB
    l_hot = np.array([a.l_hot for a in apps])
    l_comp = np.array([a.l_comp for a in apps])

    B_mb = B / MB
    n_states = 1 << k
    states = np.arange(n_states)
    A_xor = _subset_sums(a_vec)                      # anon MB inside a mask
    M_sub = _subset_sums(m_vec) + (rho_a.sum() - _subset_sums(rho_a))
    moved = lam.z * A_xor[states[:, None] ^ states[None, :]]   # [S, S']

    T = len(trace)
    V_next = lam.z * (a_vec.sum() - A_xor)            # G_T(S): anon still in zram
    argmin = np.zeros((T, n_states), dtype=np.int64)
    phi = np.vectorize(lam.phi)
    for t in range(T - 1, -1, -1):
        r = idx[trace[t]]
        bit = 1 << r
        has_r = (states & bit) != 0
        M_bg = M_sub - m_vec[r]                       # background footprint when r in S'
        feasible = has_r & (M_bg <= B_mb + 1e-9)
        feasible[bit] = True                          # {r_t} alone is always allowed
        col = np.where(feasible, lam.m * M_bg + V_next, np.inf)
        total = moved + col[None, :]                  # [S, S']
        best = np.argmin(total, axis=1)
        V = total[states, best]
        # latency term depends on the *current* state S only
        L = np.where(has_r, l_hot[r], l_comp[r])
        V = V + lam.L * phi(L)
        argmin[t] = best
        V_next = V

    S0_mask = (n_states - 1) if initial_resident is None else \
        sum(1 << idx[n] for n in initial_resident)
    opt_value = float(V_next[S0_mask])

    # roll the optimal policy forward to produce a comparable RunResult
    amap = {a.name: a for a in apps}
    S = set(names) if initial_resident is None else set(initial_resident)
    mask = S0_mask
    steps = []
    total = 0.0
    for t, req in enumerate(trace):
        nxt = int(argmin[t, mask])
        new = {names[i] for i in range(k) if nxt & (1 << i)}
        rec = step_cost(S, new, req, amap, lam, B, t)
        steps.append(rec)
        total += rec.cost
        S, mask = new, nxt
    G = terminal_cost(S, amap, lam)
    res = RunResult("bellman_opt", total + G, G, steps, None, {"V1": round(opt_value, 2)})
    assert abs(res.total_cost - opt_value) < 1e-6 * max(1.0, abs(opt_value)), \
        (res.total_cost, opt_value)
    return res, V_next
