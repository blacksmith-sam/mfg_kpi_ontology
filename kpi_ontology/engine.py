"""Evaluate KPIs, roll up the tree, evaluate sequential gates, schedule
deliverables (critical path) and run Monte Carlo predictions."""
from __future__ import annotations

import ast
import math
import operator as op
import random
from dataclasses import dataclass, field

from .model import children_index, formula_names, node, topo_order

_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
        ast.Pow: op.pow, ast.Mod: op.mod, ast.FloorDiv: op.floordiv}
_CMP = {ast.Lt: op.lt, ast.LtE: op.le, ast.Gt: op.gt, ast.GtE: op.ge, ast.Eq: op.eq, ast.NotEq: op.ne}
_FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "sqrt": math.sqrt,
          "log": math.log, "exp": math.exp, "clamp": lambda x, lo, hi: max(lo, min(hi, x))}
OPS = {">=": op.ge, ">": op.gt, "<=": op.le, "<": op.lt, "==": op.eq}

# Objective health bands on the 0..1 roll-up score
HEALTH_GREEN, HEALTH_AMBER = 0.85, 0.5


class _Missing(Exception):
    pass


def safe_eval(expr, env):
    """Evaluate an arithmetic formula without `eval`. Returns None if an input
    is missing or the math is undefined (e.g. divide by zero)."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            v = env.get(n.id)
            if v is None:
                raise _Missing(n.id)
            return v
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.IfExp):
            return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _CMP:
            return _CMP[type(n.ops[0])](ev(n.left), ev(n.comparators[0]))
        raise ValueError(f"unsupported formula element: {ast.dump(n)}")
    try:
        return ev(ast.parse(str(expr), mode="eval"))
    except (_Missing, ZeroDivisionError, ValueError, OverflowError):
        return None


def score_kpi(k, v):
    """Map a value onto 0..1 between threshold (0) and target (1) and a RAG status."""
    t = k.get("target")
    if v is None:
        return None, "no_data"
    if t is None:
        return None, "info"
    th = k.get("threshold", t)
    up = k.get("direction", "up") == "up"
    meets = v >= t if up else v <= t
    beyond = v < th if up else v > th
    s = (1.0 if meets else 0.0) if t == th else max(0.0, min(1.0, (v - th) / (t - th)))
    return s, "green" if meets else ("red" if beyond else "amber")


def health_status(s):
    if s is None:
        return "no_data"
    return "green" if s >= HEALTH_GREEN else "amber" if s >= HEALTH_AMBER else "red"


@dataclass
class Snapshot:
    values: dict
    scores: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)
    process_gates: dict = field(default_factory=dict)
    program_gates: dict = field(default_factory=dict)
    bottleneck: str | None = None


def evaluate(m, values=None, schedule_result=None):
    """Compute every KPI, roll up objectives and evaluate all gates."""
    env = {vid: v.get("value") for vid, v in m["variables"].items()}
    env.update(values or {})
    deps = {kid: ({n for n in formula_names(k["formula"]) if n in m["kpis"]} if k.get("formula") else set())
            for kid, k in m["kpis"].items()}
    for kid in topo_order(list(m["kpis"]), deps):
        k = m["kpis"][kid]
        env[kid] = safe_eval(k["formula"], env) if k.get("formula") else None

    snap = Snapshot(values=env)
    for kid, k in m["kpis"].items():
        snap.scores[kid], snap.status[kid] = score_kpi(k, env[kid])

    kids = children_index(m)

    def roll(nid):
        if nid in snap.scores and snap.scores[nid] is not None:
            return snap.scores[nid]
        if nid in m["kpis"] and not kids.get(nid):
            return None
        parts = [((node(m, c) or {}).get("weight", 1), roll(c)) for c in kids.get(nid, [])]
        parts = [(w, s) for w, s in parts if s is not None]
        s = sum(w * s for w, s in parts) / sum(w for w, _ in parts) if parts else None
        snap.scores[nid] = s
        if nid in m["objectives"] or snap.status.get(nid) in (None, "no_data", "info"):
            snap.status[nid] = health_status(s)
        return s

    for oid in m["objectives"]:
        roll(oid)
    for kid in m["kpis"]:
        if snap.scores.get(kid) is None:
            roll(kid)

    for cid, c in m["constraints"].items():
        ok = safe_eval(c["expr"], env)
        snap.constraints[cid] = None if ok is None else bool(ok)

    caps = {kid: env[kid] for kid, k in m["kpis"].items() if k.get("role") == "capacity" and env[kid] is not None}
    snap.bottleneck = min(caps, key=caps.get) if caps else None

    _eval_process_gates(m, snap)
    _eval_program_gates(m, snap, schedule_result)
    return snap


def _check(c, snap):
    if "constraint" in c:
        ok = snap.constraints.get(c["constraint"])
        return bool(ok), f"constraint {c['constraint']} holds (actual {'n/a' if ok is None else ok})"
    if "gate" in c:
        g = snap.process_gates.get(c["gate"]) or snap.program_gates.get(c["gate"]) or {}
        return g.get("status") == "pass", f"gate {c['gate']} = {g.get('status', '?')}"
    v, st = snap.values.get(c["kpi"]), snap.status.get(c["kpi"])
    if "op" in c:
        ok = v is not None and OPS[c["op"]](v, c["value"])
        return ok, f"{c['kpi']} {c['op']} {c['value']} (actual {_fmt(v)})"
    req = c.get("require", "amber")
    ok = st == "green" or (req == "amber" and st == "amber")
    return ok, f"{c['kpi']} is {req} or better (actual {st})"


def _gate_result(criteria, snap, upstream_ok, extra_hard=()):
    rows = [(c.get("hard", True), *_check(c, snap), c.get("kpi") or c.get("gate") or c.get("constraint"))
            for c in criteria or []]
    rows += list(extra_hard)
    hard_ok = all(ok for hard, ok, _, _ in rows if hard)
    readiness = sum(ok for _, ok, _, _ in rows) / len(rows) if rows else 1.0
    status = "blocked" if not upstream_ok else ("pass" if hard_ok else "fail")
    return {"status": status, "readiness": readiness,
            "criteria": [{"hard": h, "ok": ok, "text": t, "key": k} for h, ok, t, k in rows]}


def _eval_process_gates(m, snap):
    upstream_ok = True
    gates = sorted(m["process_gates"].values(), key=lambda g: m["stages"][g["stage"]].get("order", 0))
    for g in gates:
        r = _gate_result(g.get("criteria"), snap, upstream_ok)
        snap.process_gates[g["id"]] = r
        upstream_ok = r["status"] == "pass"


def _eval_program_gates(m, snap, sched):
    upstream_ok = True
    for g in sorted(m["program_gates"].values(), key=lambda g: g.get("order", 0)):
        extra = []
        for d in g.get("deliverables") or []:
            pct = m["deliverables"][d].get("pct_complete", 0)
            extra.append((True, pct >= 1.0, f"deliverable {d} complete ({pct:.0%})", d))
        r = _gate_result(g.get("criteria"), snap, upstream_ok, extra)
        if sched:
            r["projected_week"] = sched["gates"][g["id"]]
            r["planned_week"] = g.get("planned_week")
            if g.get("planned_week") is not None:
                r["slip_wks"] = sched["gates"][g["id"]] - g["planned_week"]
        snap.program_gates[g["id"]] = r
        upstream_ok = r["status"] == "pass"


def schedule(m):
    """Forward-pass on remaining work: weeks from today until each deliverable
    and program gate is done, plus the critical path to the last gate."""
    D = m["deliverables"]
    deps = {d: list(x.get("depends_on") or []) for d, x in D.items()}
    finish, driver = {}, {}
    for d in topo_order(list(D), deps):
        x = D[d]
        rem = x.get("duration_wks", 0) * (1 - x.get("pct_complete", 0))
        start = max((finish[p] for p in deps[d]), default=0.0)
        driver[d] = max(deps[d], key=lambda p: finish[p]) if deps[d] else None
        finish[d] = start + rem
    gates, prev = {}, 0.0
    gate_driver = {}
    for g in sorted(m["program_gates"].values(), key=lambda g: g.get("order", 0)):
        ds = g.get("deliverables") or []
        own = max((finish[d] for d in ds), default=0.0)
        gates[g["id"]] = max(own, prev)
        gate_driver[g["id"]] = max(ds, key=lambda d: finish[d]) if ds else None
        prev = gates[g["id"]]
    # critical path: walk back from the deliverable that finishes last
    path = []
    cur = max(finish, key=finish.get) if finish else None
    while cur:
        path.append(cur)
        cur = driver[cur]
    return {"finish": finish, "gates": gates, "gate_driver": gate_driver, "critical_path": path[::-1]}


def _sample(var, rng, base=None):
    """Draw one value. `base` (a measured value) re-centres the distribution on it."""
    d = var.get("dist")
    base = var.get("value") if base is None else base
    if not d or base is None:
        return base
    shift = base - (var.get("value") or 0) if var.get("value") is not None else 0
    t = d.get("type", "normal")
    if t == "normal":
        x = rng.gauss(base, d["sd"])
    elif t == "triangular":
        x = rng.triangular(d["low"] + shift, d["high"] + shift, d.get("mode", var.get("value")) + shift)
    elif t == "uniform":
        x = rng.uniform(d["low"] + shift, d["high"] + shift)
    else:
        raise ValueError(f"{var['id']}: unknown dist type {t!r}")
    if "min" in d:
        x = max(d["min"], x)
    if "max" in d:
        x = min(d["max"], x)
    return round(x) if var.get("type") == "int" else x


def _q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]


def monte_carlo(m, runs=2000, seed=7, overrides=None):
    """Sample every variable that has a `dist`, re-evaluate the whole model and
    report P10/P50/P90 per KPI, P(green) and gate pass probabilities.
    `overrides` (measured values) stay fixed unless the variable has a `dist`;
    then the distribution is centred on the measured value ("what could next week look like")."""
    rng = random.Random(seed)
    sched = schedule(m)
    overrides = overrides or {}
    stochastic = [v for v in m["variables"].values() if v.get("dist")]
    vals = {k: [] for k in m["kpis"]}
    green = {k: 0 for k in m["kpis"]}
    gpass = {g: 0 for g in list(m["process_gates"]) + list(m["program_gates"])}
    why = {g: {} for g in gpass}  # gate -> {failing hard criterion: count}
    # data-integrity rules describe the report, not the process: not meaningful on simulated weeks
    cons = {c: 0 for c, x in m["constraints"].items() if x.get("kind") != "data_integrity"}
    bottlenecks = {}
    for _ in range(runs):
        sample = dict(overrides)
        sample.update({v["id"]: _sample(v, rng, overrides.get(v["id"])) for v in stochastic})
        s = evaluate(m, sample, sched)
        for k in m["kpis"]:
            vals[k].append(s.values[k])
            green[k] += s.status[k] == "green"
        for g, r in {**s.process_gates, **s.program_gates}.items():
            gpass[g] += r["status"] == "pass"
            reasons = (["(blocked by upstream gate)"] if r["status"] == "blocked" else
                       [c["key"] for c in r["criteria"] if c["hard"] and not c["ok"]])
            for k in reasons:
                why[g][k] = why[g].get(k, 0) + 1
        for c in cons:
            cons[c] += bool(s.constraints.get(c))
        bottlenecks[s.bottleneck] = bottlenecks.get(s.bottleneck, 0) + 1
    kpis = {k: {"p10": _q(v, .1), "p50": _q(v, .5), "p90": _q(v, .9), "p_green": green[k] / runs}
            for k, v in vals.items()}
    return {"runs": runs, "seed": seed, "stochastic_vars": [v["id"] for v in stochastic], "kpis": kpis,
            "gate_pass_prob": {g: n / runs for g, n in gpass.items()},
            "gate_fail_reasons": {g: {k: n / runs for k, n in sorted(r.items(), key=lambda x: -x[1])} for g, r in why.items()},
            "constraint_hold_prob": {c: n / runs for c, n in cons.items()},
            "bottleneck_prob": {b: n / runs for b, n in bottlenecks.items()}}


def _fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    a = abs(v)
    if float(v).is_integer() and a >= 1:
        return f"{int(v):,}"
    if a >= 10000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:,.1f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}".rstrip("0").rstrip(".") if v else "0"


fmt = _fmt
