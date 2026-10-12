"""Week-over-week runs: evaluate every run file, trend and SPC-flag each KPI,
and propose constraints, objectives, KPIs and calibrated targets/distributions
from what the weekly reports actually show."""
from __future__ import annotations

import glob
import os
import statistics as st

import yaml

from . import engine
from .engine import fmt
from .export import ANSI, TAG, _c


# ----------------------------------------------------------------- loading
def load_runs(run_dir):
    runs = []
    for p in sorted(glob.glob(os.path.join(run_dir, "*.yaml"))):
        with open(p) as fh:
            doc = yaml.safe_load(fh) or {}
        if isinstance(doc.get("values"), dict):
            doc["_path"] = p
            doc.setdefault("period", os.path.splitext(os.path.basename(p))[0])
            runs.append(doc)
    return sorted(runs, key=lambda r: str(r["period"]))


def evaluate_runs(m, runs):
    sched = engine.schedule(m)
    return [engine.evaluate(m, {k: v for k, v in r["values"].items() if v is not None}, sched) for r in runs]


# -------------------------------------------------------------------- SPC
def spc(xs):
    """Individuals (XmR) chart limits: centre, UCL, LCL from the moving range."""
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return None
    mr = [abs(a - b) for a, b in zip(xs[1:], xs[:-1])]
    sigma = (sum(mr) / len(mr)) / 1.128
    c = st.mean(xs)
    return {"centre": c, "ucl": c + 3 * sigma, "lcl": c - 3 * sigma, "sigma": sigma}


def trend(xs, up=True):
    """Least-squares slope over the last 4 points, in the 'good' direction."""
    xs = [x for x in xs if x is not None][-4:]
    if len(xs) < 3:
        return "  "
    n = len(xs)
    mx = (n - 1) / 2
    my = st.mean(xs)
    slope = sum((i - mx) * (x - my) for i, x in enumerate(xs)) / sum((i - mx) ** 2 for i in range(n))
    scale = max(abs(my), 1e-9)
    if abs(slope) / scale < 0.005:
        return "→ "
    return "↑ " if (slope > 0) == up else "↓ "


# ------------------------------------------------------------------- text
def text_history(m, runs, snaps, color=True, last=8, only=None):
    runs, snaps = runs[-last:], snaps[-last:]
    weeks = [str(r["period"])[-3:] for r in runs]
    out = [_c(f"RUN HISTORY  {runs[0]['period']} .. {runs[-1]['period']}  ({len(runs)} runs)", "bold", color),
           "  trend: ↑ improving, ↓ worsening, → flat (last 4 runs).  SPC vs earlier runs' 3-sigma limits: '!' adverse shift, '+' favourable shift", ""]
    head = f"  {'':<44}" + "".join(f"{w:>9}" for w in weeks) + "   trend SPC"
    out.append(head)

    def row(label, vals, sts, up=True, flag=""):
        cells = ""
        for v, s in zip(vals, sts):
            txt = f"{fmt(v):>7}{TAG.get(s, ' ')}" if v is not None else f"{'n/a':>8}"
            cells += " " + _c(txt, s, color) if s in ANSI else " " + txt
        return f"  {label[:43]:<44}{cells}   {trend(vals, up)}   {flag}"

    root_kids = [o for o, n in m["objectives"].items() if n.get("parent") is None or
                 m["objectives"].get(n["parent"], {}).get("parent") is None]
    out.append(_c("  Objectives (roll-up score)", "bold", color))
    for o in root_kids:
        out.append(row(m["objectives"][o]["name"], [s.scores.get(o) for s in snaps], [s.status.get(o) for s in snaps]))
    out.append("")
    out.append(_c("  KPIs", "bold", color))
    for kid, k in m["kpis"].items():
        if k.get("target") is None or (only and kid not in only):
            continue
        vals = [s.values.get(kid) for s in snaps]
        if all(v is None for v in vals) or len({round(v, 9) for v in vals if v is not None}) == 1:
            continue  # constant across runs = not coming from the reports
        up = k.get("direction", "up") == "up"
        lim, flag = spc(vals[:-1]), ""
        if lim and vals[-1] is not None:
            if vals[-1] > lim["ucl"]:
                flag = "+" if up else "!"
            elif vals[-1] < lim["lcl"]:
                flag = "!" if up else "+"
        out.append(row(k["name"], vals, [s.status.get(kid) for s in snaps], up, flag))
    out.append("")
    out.append(_c("  Process gates", "bold", color))
    for g in m["process_gates"]:
        out.append(f"  {m['process_gates'][g]['name'][:43]:<44}" + "".join(
            " " + _c(f"{s.process_gates[g]['status'][:4].upper():>8}", s.process_gates[g]["status"], color) for s in snaps))
    out.append("")
    bad = [(r["period"], c) for r, s in zip(runs, snaps) for c, ok in s.constraints.items() if ok is False]
    out.append(_c("  Constraint violations", "bold", color))
    out += [f"    {p}: {m['constraints'][c]['name']}" for p, c in bad] or ["    none"]
    out.append("")
    gaps = runs[-1].get("not_in_report") or []
    if gaps:
        out.append(f"  Latest run: {len(gaps)} model variables are not in the report and use model baseline values. "
                   "Run `questions --run` to get the questions for them.")
    return "\n".join(out)


# ---------------------------------------------------------------- propose
def _parent_objective(m, kid):
    cur = m["kpis"].get(kid, {}).get("parent")
    while cur and cur not in m["objectives"]:
        cur = m["kpis"][cur].get("parent")
    return cur


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]


def propose(m, runs, snaps):
    """Build a YAML extension (as a dict) from the run history. Nothing is applied:
    review it, then move it to extensions/."""
    latest = runs[-1]
    notes = []
    doc = {"domains": [], "objectives": [], "variables": [], "kpis": [], "constraints": []}

    # 1. constraints from the targets the CM states in its report
    for key, c in (latest.get("commitments") or {}).items():
        target = m["kpis"].get(key) or m["variables"].get(key)
        if target is None:
            notes.append(f"commitment {key!r} does not match a KPI or variable; skipped")
            continue
        history = [r.get("commitments", {}).get(key, {}).get("value") for r in runs]
        changed = len({h for h in history if h is not None}) > 1
        vals = [s.values.get(key) for s in snaps]
        held = sum(1 for v in vals if v is not None and engine.OPS[c["op"]](v, c["value"]))
        doc["constraints"].append({
            "id": f"c_cm_{key}", "name": f"CM commitment: {target.get('name', key)} {c['op']} {fmt(c['value'])}",
            "kind": "contract", "hard": bool(c.get("hard", True)), "expr": f"{key} {c['op']} {c['value']}",
            "objective": _parent_objective(m, key) if key in m["kpis"] else None,
            "source": f"CM weekly report {latest['period']} ({c['where']})",
            "evidence": f"held in {held}/{len(vals)} runs" + ("; CM changed this target during the period" if changed else ""),
        })

    # 2. new variables + KPIs (+ objective) for report numbers the model does not use yet
    extras = latest.get("extra") or {}
    if extras:
        doc["domains"].append({"id": "cm_performance", "name": "Contract Manufacturer Performance", "type": "business"})
        doc["objectives"].append({"id": "obj_cm_performance", "parent": "root_pilot_line", "weight": 1,
                                  "name": "Contract manufacturer performance (from weekly report)"})
    for key, e in extras.items():
        series = [r.get("extra", {}).get(key, {}).get("value") for r in runs]
        series = [x for x in series if x is not None]
        down = e.get("direction", "down") == "down"
        var = {"id": key, "value": e["value"], "unit": e.get("unit"), "domain": "cm_performance",
               "source": "CM weekly report", "ask": "CM", "question": f"What was '{e.get('name', key)}' this week?"}
        if len(series) >= 3 and st.pstdev(series) > 0:
            var["dist"] = {"type": "normal", "sd": round(st.pstdev(series), 4), "min": 0}
        doc["variables"].append(var)
        best = _q(series, 0.25 if down else 0.75)
        worst = _q(series, 0.9 if down else 0.1)
        tgt = e.get("target", best)
        thr = worst if (worst > tgt if down else worst < tgt) else (tgt * 2 if down else tgt / 2)
        doc["kpis"].append({"id": f"kpi_{key}", "parent": "obj_cm_performance", "name": e.get("name", key),
                            "domain": "cm_performance", "formula": key, "unit": e.get("unit"),
                            "direction": "down" if down else "up", "target": tgt, "threshold": round(thr, 4),
                            "kind": "lagging", "proposal_note": ("target = CM's stated target" if "target" in e else
                                                                 "target = best quartile of history") +
                            f"; threshold = worst decile of {len(series)} runs"})

    # 3. recalibrate thresholds/targets from demonstrated capability
    for kid, k in m["kpis"].items():
        if k.get("target") is None:
            continue
        xs = [s.values.get(kid) for s in snaps if s.values.get(kid) is not None]
        if len(xs) < 4 or len(set(xs)) == 1:
            continue
        up = k.get("direction", "up") == "up"
        best, floor = _q(xs, 0.75 if up else 0.25), _q(xs, 0.10 if up else 0.90)
        better = (lambda a, b: a > b) if up else (lambda a, b: a < b)
        change = {}
        if not better(best, k["target"]) and best != k["target"] and not any(better(x, k["target"]) or x == k["target"] for x in xs):
            change["proposal_note"] = f"target {fmt(k['target'])} never reached in {len(xs)} runs (best {fmt(max(xs) if up else min(xs))}): stretch target, keep only if a plan exists to close the gap"
        if better(floor, k["target"]) or floor == k["target"]:
            change["proposal_note"] = (f"worst decile of {len(xs)} runs ({fmt(floor)}) already meets target {fmt(k['target'])}: "
                                       "target may be too lax - set it from the pilot need, not from history")
        elif k.get("threshold") is not None and better(floor, k["threshold"]):
            change["threshold"] = round(floor, 6)
            change["proposal_note"] = (change.get("proposal_note", "") + "; " if change.get("proposal_note") else "") + \
                f"threshold tightened from {fmt(k['threshold'])} to worst decile of runs ({fmt(floor)}) so a slip back is flagged"
        if change:
            doc["kpis"].append({"id": kid, **change})

    # 4. calibrate Monte Carlo spreads from real week-to-week variation
    # Only inputs the model already marks as uncertain (have a `dist`) are recalibrated:
    # sampling linked volume counts independently (e.g. shipped vs. with-genealogy) creates impossible weeks.
    for vid, v in m["variables"].items():
        xs = [r["values"].get(vid) for r in runs if r["values"].get(vid) is not None]
        if not v.get("dist") or len(xs) < 4 or st.pstdev(xs) == 0:
            continue
        # detrend: spread of residuals around the last-4 trend is what the next week will look like
        resid = [b - a for a, b in zip(xs[:-1], xs[1:])]
        sd = st.pstdev(resid) / 2 ** 0.5 if len(resid) > 1 else st.pstdev(xs)
        dist = {"type": "normal", "sd": round(sd, 6)}
        for bound in ("min", "max"):
            if (v.get("dist") or {}).get(bound) is not None:
                dist[bound] = v["dist"][bound]
        if "min" not in dist:
            dist["min"] = 0
        doc["variables"].append({"id": vid, "value": xs[-1], "dist": dist,
                                 "proposal_note": f"value = latest run; sd from week-to-week change over {len(xs)} runs"})

    doc = {k: v for k, v in doc.items() if v}
    return doc, notes


def write_proposal(doc, notes, period, path):
    header = (f"# PROPOSED extension generated from weekly runs up to {period}.\n"
              "# Nothing here is active. Review each item, delete what you do not agree with, then either:\n"
              f"#   try it:  python -m kpi_ontology report --add {os.path.relpath(path)}\n"
              "#   adopt:   move this file into extensions/\n"
              + "".join(f"# NOTE: {n}\n" for n in notes) + "\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(header)
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True, width=140)
