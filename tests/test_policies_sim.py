import itertools
import random

from cf.policies import (AppInfo, LandlordPolicy, HybridPolicy, MarkovPredictor, greedy_fill,
                         make_policy, residency, weighted_inversions)
from cf.sim.bellman import solve_opt
from cf.sim.model import MB, Lambdas, SimConfig, budget, evaluate_policy, make_apps, step_cost, terminal_cost
from cf.trace import gen_markov, gen_zipf


def infos_of(sizes):
    return {n: AppInfo(n, m * MB, 0.6 * m * MB, rho=0.3, c_resume=1.0) for n, m in sizes.items()}


def test_greedy_fill_respects_budget_and_keeps_request():
    infos = infos_of({"a": 100, "b": 200, "c": 300, "d": 400})
    S = greedy_fill("d", ["a", "b", "c", "d"], infos, budget=250 * MB, current=set(infos))
    assert "d" in S
    assert residency(S, infos, "d") <= 250 * MB
    # lowest-priority apps (end of order) are evicted first
    assert "a" in S


def test_landlord_evicts_and_refills():
    infos = infos_of({"a": 100, "b": 100, "c": 100})
    pol = LandlordPolicy(list(infos))
    S = pol.choose_resident(0, "a", infos, budget=120 * MB, current=set(infos))
    assert "a" in S and residency(S, infos, "a") <= 120 * MB
    assert pol.credit["a"] == infos["a"].c_resume
    evicted = set(infos) - S
    assert all(pol.credit[n] <= 1e-9 for n in evicted)


def test_hybrid_weight_decays_with_bad_predictions():
    apps = [f"a{i}" for i in range(4)]
    pol = HybridPolicy(apps, H=2)
    infos = infos_of({a: 100 for a in apps})
    rng = random.Random(3)
    for t in range(60):
        req = apps[rng.randrange(4)]
        pol.on_request(t, req)
        pol.choose_resident(t, req, infos, 150 * MB, set(apps))
    assert 0.0 <= pol.w <= 1.0
    assert pol._errs, "prediction error should have been scored"


def test_markov_predictor_probabilities():
    apps = ["x", "y", "z"]
    pred = MarkovPredictor(apps, H=2, alpha=0.01)
    for r in ["x", "y", "x", "y", "x", "y", "x", "y"]:
        pred.observe(r)
    p, tau = pred.hit_probs("x")
    assert p["y"] > 0.9 and p["z"] < 0.1
    assert tau["y"] < tau["z"]


def test_weighted_inversions():
    assert weighted_inversions({"a": 1, "b": 2}, {"a": 1, "b": 2}) == 0.0
    assert weighted_inversions({"a": 1, "b": 2}, {"a": 2, "b": 1}) == 1.0


def brute_force_opt(trace, apps, lam, B):
    names = [a.name for a in apps]
    amap = {a.name: a for a in apps}
    infos = {a.name: AppInfo(a.name, a.m, a.a, a.rho) for a in apps}
    subsets = [set(c) for r in range(len(names) + 1) for c in itertools.combinations(names, r)]
    best = float("inf")
    for choice in itertools.product(range(len(subsets)), repeat=len(trace)):
        S = set(names)
        tot = 0.0
        ok = True
        for t, (req, ci) in enumerate(zip(trace, choice)):
            new = subsets[ci]
            if req not in new or (residency(new, infos, req) > B and new != {req}):
                ok = False
                break
            tot += step_cost(S, new, req, amap, lam, B, t).cost
            S = new
        if ok:
            tot += terminal_cost(S, amap, lam)
            best = min(best, tot)
    return best


def test_bellman_matches_brute_force_small():
    cfg = SimConfig(k=3, eta=0.5, seed=7)
    apps = make_apps(cfg)
    lam = Lambdas()
    B = budget(cfg, apps)
    trace = gen_zipf([a.name for a in apps], 4, seed=2)
    opt, _ = solve_opt(trace, apps, lam, B)
    bf = brute_force_opt(trace, apps, lam, B)
    assert abs(opt.total_cost - bf) < 1e-6 * max(1.0, bf)


def test_bellman_lower_bounds_online_policies():
    for seed in range(3):
        cfg = SimConfig(k=6, eta=0.45, seed=seed)
        apps = make_apps(cfg)
        names = [a.name for a in apps]
        lam = Lambdas()
        B = budget(cfg, apps)
        trace = gen_markov(names, 80, seed=seed)
        opt, _ = solve_opt(trace, apps, lam, B)
        for pname in ["lru", "lfu", "landlord", "markov", "hybrid"]:
            res = evaluate_policy(make_policy(pname, names), trace, apps, lam, B)
            assert res.total_cost >= opt.total_cost - 1e-6, (pname, res.total_cost, opt.total_cost)
            assert all(s.req in s.resident for s in res.steps)


def test_trace_generators():
    apps = [f"a{i}" for i in range(5)]
    z = gen_zipf(apps, 50, seed=1)
    assert len(z) == 50 and all(z[i] != z[i + 1] for i in range(49))
    m = gen_markov(apps, 30, seed=1, drift_at=15)
    assert len(m) == 30 and set(m) <= set(apps)
