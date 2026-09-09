"""Cost model of the paper restricted to the "first paper" setting:
fixed working sets, two tiers (DRAM / ZRAM), indivisible app objects.

State at the start of step t : S_{t-1}  (set of apps resident in DRAM)
Action                       : S_t      (new resident set, must contain r_t)
Stage cost
    l_t = lambda_z * (Zin_t + Zout_t)           bytes moved into / out of ZRAM
        + lambda_m * M(S_t)                     background DRAM footprint
        + lambda_L * phi(L_t)                   resume latency of r_t (depends on S_{t-1})
Terminal cost  G_T = lambda_z * sum_{i not in S_T} a_i  (everything must come back eventually)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict
from typing import Optional, Sequence

from ..policies import AppInfo, Policy, residency, weighted_inversions

MB = 1024 * 1024


@dataclass
class SimApp:
    name: str
    m: float          # bytes, anon + file working set
    a: float          # bytes, anon part
    rho: float        # zram ratio
    l_hot: float      # ms, resume when resident
    l_comp: float     # ms, resume when anon pages sit in zram
    l_cold: float     # ms, cold start (used only for reference / kills)

    def info(self, lam: "Lambdas") -> AppInfo:
        c = lam.z * 2 * self.a / MB + lam.L * (self.l_comp - self.l_hot)
        return AppInfo(name=self.name, m=self.m, a=self.a, rho=self.rho, c_resume=c)


@dataclass
class Lambdas:
    z: float = 1.0        # per MB moved
    m: float = 0.02       # per MB resident per step
    L: float = 1.0        # per ms of resume latency (phi = identity) ...
    L_max: Optional[float] = None  # ... or phi = [L - L_max]_+^2 (ms) when set

    def phi(self, L: float) -> float:
        if self.L_max is None:
            return L
        return max(L - self.L_max, 0.0) ** 2


@dataclass
class SimConfig:
    k: int = 8
    eta: float = 0.1
    lam: Lambdas = field(default_factory=Lambdas)
    seed: int = 0
    m_range_mb: tuple[float, float] = (120, 450)
    anon_share: tuple[float, float] = (0.5, 0.8)
    rho_range: tuple[float, float] = (0.25, 0.45)
    l_hot_ms: tuple[float, float] = (80, 250)
    decompress_mb_per_ms: float = 1.2   # ~1.2 GB/s effective zram read-back incl. faults
    comp_fixed_ms: float = 60
    budget_override_bytes: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def make_apps(cfg: SimConfig) -> list[SimApp]:
    rng = random.Random(cfg.seed)
    apps = []
    for i in range(cfg.k):
        m = rng.uniform(*cfg.m_range_mb) * MB
        a = m * rng.uniform(*cfg.anon_share)
        rho = rng.uniform(*cfg.rho_range)
        l_hot = rng.uniform(*cfg.l_hot_ms)
        l_comp = l_hot + cfg.comp_fixed_ms + (a / MB) / cfg.decompress_mb_per_ms
        apps.append(SimApp(f"app{i:02d}", m, a, rho, l_hot, l_comp, l_cold=l_comp * 2.5))
    return apps


def budget(cfg: SimConfig, apps: Sequence[SimApp]) -> float:
    """B = eta * sum_i m_fg_i  (m_fg_i approximated by the working set m_i)."""
    if cfg.budget_override_bytes is not None:
        return cfg.budget_override_bytes
    return cfg.eta * sum(a.m for a in apps)


@dataclass
class StepRecord:
    t: int
    req: str
    resident: list[str]
    zin_mb: float
    zout_mb: float
    M_mb: float
    latency_ms: float
    cost: float
    budget_violation: bool
    from_zram: bool


def step_cost(prev: set[str], new: set[str], req: str, apps: dict[str, SimApp], lam: Lambdas,
              B: float, t: int) -> StepRecord:
    zin = sum(apps[n].a for n in prev - new)
    zout = sum(apps[n].a for n in new - prev)
    infos = {n: AppInfo(n, a.m, a.a, a.rho) for n, a in apps.items()}
    M = residency(new, infos, req)
    from_zram = req not in prev
    L = apps[req].l_comp if from_zram else apps[req].l_hot
    cost = lam.z * (zin + zout) / MB + lam.m * M / MB + lam.L * lam.phi(L)
    return StepRecord(t, req, sorted(new), zin / MB, zout / MB, M / MB, L, cost,
                      M > B + 1e-6, from_zram)


def terminal_cost(S: set[str], apps: dict[str, SimApp], lam: Lambdas) -> float:
    return lam.z * sum(a.a for n, a in apps.items() if n not in S) / MB


@dataclass
class RunResult:
    policy: str
    total_cost: float
    terminal_cost: float
    steps: list[StepRecord]
    eta_rank: Optional[float] = None
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        lat = sorted(s.latency_ms for s in self.steps)
        T = len(self.steps)

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            return lat[min(T - 1, int(round(p * (T - 1))))]
        return {
            "policy": self.policy,
            "total_cost": round(self.total_cost, 2),
            "zin_mb": round(sum(s.zin_mb for s in self.steps), 1),
            "zout_mb": round(sum(s.zout_mb for s in self.steps), 1),
            "avg_M_mb": round(sum(s.M_mb for s in self.steps) / max(T, 1), 1),
            "peak_M_mb": round(max((s.M_mb for s in self.steps), default=0.0), 1),
            "lat_p50_ms": round(pct(0.5), 1), "lat_p95_ms": round(pct(0.95), 1),
            "lat_p99_ms": round(pct(0.99), 1),
            "zram_resumes": sum(1 for s in self.steps if s.from_zram),
            "budget_violations": sum(1 for s in self.steps if s.budget_violation),
            "eta_rank": None if self.eta_rank is None else round(self.eta_rank, 4),
            **self.extra,
        }


def evaluate_policy(policy: Policy, trace: Sequence[str], apps: Sequence[SimApp], lam: Lambdas,
                    B: float, initial_resident: Optional[set[str]] = None) -> RunResult:
    amap = {a.name: a for a in apps}
    infos = {a.name: a.info(lam) for a in apps}
    S = set(initial_resident) if initial_resident is not None else set(amap)
    steps: list[StepRecord] = []
    total = 0.0
    tau_hats: list[tuple[int, dict[str, float]]] = []
    for t, req in enumerate(trace):
        policy.on_request(t, req)
        new = policy.choose_resident(t, req, infos, B, S)
        new = set(new) | {req}
        rec = step_cost(S, new, req, amap, lam, B, t)
        steps.append(rec)
        total += rec.cost
        th = policy.predicted_next_use(req)
        if th:
            tau_hats.append((t, dict(th)))
        S = new
    G = terminal_cost(S, amap, lam)
    eta = None
    if tau_hats:
        errs = []
        for t, th in tau_hats:
            truth = {}
            for n in th:
                nxt = next((k for k in range(t + 1, len(trace)) if trace[k] == n), None)
                truth[n] = (nxt - t) if nxt is not None else len(trace) + 1
            errs.append(weighted_inversions(th, truth, {n: infos[n].c_resume for n in th}))
        eta = sum(errs) / len(errs)
    return RunResult(policy.name, total + G, G, steps, eta)
