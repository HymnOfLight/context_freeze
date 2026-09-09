"""Application-level online policies (shared by the simulator and the on-device runner).

A policy decides, at each request t, the set S_t of apps that stay *resident in DRAM*
(the current request r_t always belongs to S_t). Every app outside S_t is frozen and
its anonymous pages are pushed to ZRAM.  Residency must satisfy

    M(S) = sum_{i in S} m_i + sum_{i not in S} rho_i * a_i  <=  B.

Policies:
  none        keep everything resident (ignores the budget) - baseline "不冻结"
  lru / lfu   classic recency / frequency ordering
  landlord    Landlord / GreedyDual-Size credits (robust, no prediction)
  markov      first-order Markov predictor, keep apps with highest p_hat * C_resume
  hybrid      Landlord credits + prediction (credits only raised; weight decays with
              observed ranking error)  -> consistency + robustness
  belady      offline: keep apps with nearest next use (needs the full trace; sim only)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class AppInfo:
    name: str
    m: float            # working set (anon + file) bytes
    a: float            # anon bytes
    rho: float = 0.35   # zram physical / logical ratio
    c_resume: float = 1.0  # cost of one freeze -> compress -> resume cycle (policy currency)
    b_keep: float = 0.0    # file-backed bytes that stay resident when the app is compressed
                           # (reclaim_mode=anon leaves them; =all drops them -> 0)

    @property
    def compressed_footprint(self) -> float:
        return self.rho * self.a + self.b_keep

    @property
    def freed_if_compressed(self) -> float:
        return max(self.m - self.compressed_footprint, 1.0)


def residency(S: set[str], infos: dict[str, AppInfo], fg: Optional[str] = None) -> float:
    """Background footprint M(S): the foreground app `fg` is excluded (B is a *background* budget)."""
    return sum(i.m if n in S else i.compressed_footprint for n, i in infos.items() if n != fg)


def greedy_fill(req: str, order: Sequence[str], infos: dict[str, AppInfo], budget: float,
                current: Optional[set[str]] = None, prefetch: bool = False) -> set[str]:
    """Demand-paging eviction: start from the current resident set plus r_t and evict apps
    from the *end* of `order` (lowest priority first) until M(S) <= B.
    With prefetch=True, remaining slack is filled with the highest-priority compressed apps
    (this costs Zout now and only pays off if the prediction is right)."""
    S = (set(current) if current is not None else set(infos)) | {req}
    S &= set(infos)
    rank = {n: k for k, n in enumerate(order)}
    while residency(S, infos, req) > budget and len(S) > 1:
        victim = max((n for n in S if n != req), key=lambda n: rank.get(n, len(rank)))
        S.discard(victim)
    if prefetch:
        for n in order:
            if n in S or n not in infos:
                continue
            S.add(n)
            if residency(S, infos, req) > budget:
                S.discard(n)
    return S


class Policy:
    name = "base"

    def __init__(self, apps: Sequence[str], **kw):
        self.apps = list(apps)
        self.t = 0
        self.kw = kw

    def on_request(self, t: int, req: str) -> None:
        self.t = t

    def choose_resident(self, t: int, req: str, infos: dict[str, AppInfo], budget: float,
                        current: set[str]) -> set[str]:
        raise NotImplementedError

    def predicted_next_use(self, req: str) -> Optional[dict[str, float]]:
        """tau_hat_i: predicted steps until next use (for eta_rank); None if no predictor."""
        return None


class NonePolicy(Policy):
    name = "none"

    def choose_resident(self, t, req, infos, budget, current):
        return set(infos)


class LRUPolicy(Policy):
    name = "lru"

    def __init__(self, apps, **kw):
        super().__init__(apps, **kw)
        self.last: dict[str, int] = {a: -1 for a in apps}

    def on_request(self, t, req):
        super().on_request(t, req)
        self.last[req] = t

    def choose_resident(self, t, req, infos, budget, current):
        order = sorted(infos, key=lambda n: -self.last.get(n, -1))
        return greedy_fill(req, order, infos, budget, current)


class LFUPolicy(Policy):
    name = "lfu"

    def __init__(self, apps, decay: float = 0.98, **kw):
        super().__init__(apps, **kw)
        self.decay = decay
        self.freq: dict[str, float] = {a: 0.0 for a in apps}

    def on_request(self, t, req):
        super().on_request(t, req)
        for k in self.freq:
            self.freq[k] *= self.decay
        self.freq[req] = self.freq.get(req, 0.0) + 1.0

    def choose_resident(self, t, req, infos, budget, current):
        order = sorted(infos, key=lambda n: -self.freq.get(n, 0.0))
        return greedy_fill(req, order, infos, budget, current)


class LandlordPolicy(Policy):
    """Landlord (Young 2002) with object size = freed DRAM, cost = c_resume.

    credit_i in [0, c_i]; on hit credit is refilled to c_i; when room is needed
    delta = min_j credit_j / size_j is charged to every resident object and zero-credit
    objects are evicted (compressed) first.
    """
    name = "landlord"

    def __init__(self, apps, **kw):
        super().__init__(apps, **kw)
        self.credit: dict[str, float] = {}

    def refill(self, req: str, infos: dict[str, AppInfo]) -> None:
        self.credit[req] = infos[req].c_resume

    def _prediction_boost(self, infos: dict[str, AppInfo]) -> None:
        pass  # hook for HybridPolicy

    def choose_resident(self, t, req, infos, budget, current):
        for n, i in infos.items():
            self.credit.setdefault(n, i.c_resume)
        self.refill(req, infos)
        self._prediction_boost(infos)
        S = set(current) | {req}
        S &= set(infos)
        # charge rent until the constraint is satisfied
        while residency(S, infos, req) > budget and len(S) > 1:
            cand = [n for n in S if n != req]
            delta = min(self.credit[n] / infos[n].freed_if_compressed for n in cand)
            for n in cand:
                self.credit[n] = max(0.0, self.credit[n] - delta * infos[n].freed_if_compressed)
            zero = sorted((n for n in cand if self.credit[n] <= 1e-12), key=self.tiebreak)
            if not zero:
                zero = [min(cand, key=lambda n: self.credit[n])]
            S.discard(zero[0])
        return S

    def tiebreak(self, n: str) -> float:
        return 0.0


class MarkovPredictor:
    """First-order Markov chain over app requests with Laplace smoothing.

    p_hat(i | cur, H) = Pr[i requested within the next H steps | current app = cur]
    computed exactly by treating i as absorbing.
    """

    def __init__(self, apps: Sequence[str], H: int = 3, alpha: float = 0.5, decay: float = 1.0):
        self.apps = list(apps)
        self.idx = {a: k for k, a in enumerate(self.apps)}
        n = len(self.apps)
        self.N = [[alpha] * n for _ in range(n)]
        self.H = H
        self.decay = decay
        self.prev: Optional[str] = None

    def observe(self, req: str) -> None:
        if req not in self.idx:
            return
        if self.prev is not None:
            r = self.N[self.idx[self.prev]]
            if self.decay < 1.0:
                for j in range(len(r)):
                    r[j] *= self.decay
            r[self.idx[req]] += 1.0
        self.prev = req

    def transition(self) -> list[list[float]]:
        return [[x / sum(row) for x in row] for row in self.N]

    def hit_probs(self, cur: str) -> tuple[dict[str, float], dict[str, float]]:
        """Returns (p_hat_i within H, expected first-hit time tau_hat_i (capped at H+1))."""
        Pm = self.transition()
        n = len(self.apps)
        c = self.idx.get(cur, 0)
        p_hat, tau_hat = {}, {}
        for target in range(n):
            # f[h][x] = Pr[hit target within h steps starting at x]
            f_prev = [0.0] * n
            first_hit: list[float] = []
            for h in range(1, self.H + 1):
                f = [0.0] * n
                for x in range(n):
                    f[x] = sum(Pm[x][y] * (1.0 if y == target else f_prev[y]) for y in range(n))
                first_hit.append(f[c] - f_prev[c])
                f_prev = f
            p = f_prev[c]
            tau = sum((h + 1) * fh for h, fh in enumerate(first_hit)) + (self.H + 1) * (1 - p)
            p_hat[self.apps[target]] = p
            tau_hat[self.apps[target]] = tau
        return p_hat, tau_hat


class MarkovPolicy(Policy):
    name = "markov"

    def __init__(self, apps, H: int = 3, prefetch: bool = False, **kw):
        super().__init__(apps, **kw)
        self.pred = MarkovPredictor(apps, H=H)
        self.prefetch = prefetch
        self._last_tau: Optional[dict[str, float]] = None

    def on_request(self, t, req):
        super().on_request(t, req)
        self.pred.observe(req)

    def choose_resident(self, t, req, infos, budget, current):
        p_hat, tau_hat = self.pred.hit_probs(req)
        self._last_tau = tau_hat
        order = sorted(infos, key=lambda n: -p_hat.get(n, 0.0) * infos[n].c_resume)
        return greedy_fill(req, order, infos, budget, current, prefetch=self.prefetch)

    def predicted_next_use(self, req):
        return self._last_tau


class HybridPolicy(LandlordPolicy):
    """Landlord credits + Markov prediction.

    Prediction may only *raise* a credit (within [credit, c_i]), which preserves Landlord's
    worst-case guarantee, and its weight w in [0, 1] decays when the observed weighted
    ranking error eta_rank grows (drift / bad predictor -> pure Landlord).
    """
    name = "hybrid"

    def __init__(self, apps, H: int = 3, w0: float = 1.0, err_window: int = 10, **kw):
        super().__init__(apps, **kw)
        self.pred = MarkovPredictor(apps, H=H)
        self.w = w0
        self.w0 = w0
        self.err_window = err_window
        self._pending: list[tuple[int, dict[str, float]]] = []  # (t, tau_hat) awaiting truth
        self._errs: list[float] = []
        self._p_hat: dict[str, float] = {}
        self._last_tau: Optional[dict[str, float]] = None
        self._history: list[str] = []
        self._weights: dict[str, float] = {}

    def on_request(self, t, req):
        super().on_request(t, req)
        self.pred.observe(req)
        self._history.append(req)
        self._score_pending()

    def _score_pending(self) -> None:
        # a prediction made at time s can be scored once every app has been seen again
        # or H+1 steps have elapsed; we use a simple normalized pairwise inversion rate
        still = []
        for s, tau_hat in self._pending:
            if self.t - s < self.pred.H + 1:
                still.append((s, tau_hat))
                continue
            truth = {}
            for a in tau_hat:
                nxt = next((k for k in range(s + 1, len(self._history)) if self._history[k] == a), None)
                truth[a] = (nxt - s) if nxt is not None else self.pred.H + 1
            self._errs.append(weighted_inversions(tau_hat, truth, self._weights))
            self._errs = self._errs[-self.err_window:]
        self._pending = still
        if self._errs:
            err = sum(self._errs) / len(self._errs)
            self.w = self.w0 * max(0.0, 1.0 - 2.0 * err)  # err 0 -> full trust, err>=0.5 -> none

    def choose_resident(self, t, req, infos, budget, current):
        self._p_hat, tau = self.pred.hit_probs(req)
        self._last_tau = tau
        self._weights = {n: i.c_resume for n, i in infos.items()}
        self._pending.append((t, dict(tau)))
        return super().choose_resident(t, req, infos, budget, current)

    def _prediction_boost(self, infos):
        for n, i in infos.items():
            boost = self.w * self._p_hat.get(n, 0.0) * i.c_resume
            self.credit[n] = min(i.c_resume, max(self.credit[n], boost))

    def tiebreak(self, n):
        return -self._p_hat.get(n, 0.0)

    def predicted_next_use(self, req):
        return self._last_tau


class BeladyPolicy(Policy):
    """Offline furthest-in-future heuristic (not optimal with sizes, but a strong reference)."""
    name = "belady"

    def __init__(self, apps, trace: Sequence[str] = (), **kw):
        super().__init__(apps, **kw)
        self.trace = list(trace)

    def choose_resident(self, t, req, infos, budget, current):
        def next_use(n: str) -> int:
            for k in range(t + 1, len(self.trace)):
                if self.trace[k] == n:
                    return k
            return 10 ** 9
        order = sorted(infos, key=next_use)
        return greedy_fill(req, order, infos, budget, current)


def weighted_inversions(tau_hat: dict[str, float], tau: dict[str, float],
                        weights: Optional[dict[str, float]] = None) -> float:
    """eta_rank: normalized weighted pairwise inversion count between predicted and true
    next-use orderings. w_ij = w_i + w_j (defaults to 1)."""
    names = [n for n in tau_hat if n in tau]
    tot = 0.0
    bad = 0.0
    for x in range(len(names)):
        for y in range(x + 1, len(names)):
            i, j = names[x], names[y]
            w = (weights.get(i, 1.0) + weights.get(j, 1.0)) if weights else 1.0
            tot += w
            if (tau_hat[i] - tau_hat[j]) * (tau[i] - tau[j]) < 0:
                bad += w
    return bad / tot if tot else 0.0


POLICIES = {
    "none": NonePolicy,
    "lru": LRUPolicy,
    "lfu": LFUPolicy,
    "landlord": LandlordPolicy,
    "markov": MarkovPolicy,
    "hybrid": HybridPolicy,
    "belady": BeladyPolicy,
}


def make_policy(name: str, apps: Sequence[str], **kw) -> Policy:
    if name not in POLICIES:
        raise ValueError(f"unknown policy {name}; choose from {sorted(POLICIES)}")
    return POLICIES[name](apps, **kw)
