"""Render the KPI ontology: terminal tree, Mermaid, HTML report, RDF/Turtle,
CSV KPI dictionary and JSON snapshot."""
from __future__ import annotations

import csv
import html
import json
import re

from .engine import fmt
from .model import children_index, formula_names, node

ANSI = {"green": "\033[32m", "amber": "\033[33m", "red": "\033[31m", "no_data": "\033[90m", "info": "\033[36m",
        "pass": "\033[32m", "fail": "\033[31m", "blocked": "\033[90m", "reset": "\033[0m", "bold": "\033[1m"}
TAG = {"green": "G", "amber": "A", "red": "R", "no_data": "-", "info": "i"}


def _c(s, color, on):
    return f"{ANSI[color]}{s}{ANSI['reset']}" if on and color in ANSI else s


def _roots(m):
    return [o for o, n in m["objectives"].items() if not n.get("parent")]


def _target_text(k):
    if k.get("target") is None:
        return ""
    arrow = ">=" if k.get("direction", "up") == "up" else "<="
    th = k.get("threshold")
    return f"tgt {arrow}{fmt(k['target'])}" + (f" | min {fmt(th)}" if th is not None and th != k["target"] else "")


# --------------------------------------------------------------------- text
def text_tree(m, snap, color=True, domain=None):
    kids = children_index(m)
    out = []

    def visible(nid):
        if domain is None:
            return True
        return node(m, nid).get("domain") == domain or any(visible(c) for c in kids.get(nid, []))

    def walk(nid, prefix, last, depth):
        n = node(m, nid)
        st = snap.status.get(nid, "no_data")
        is_obj = nid in m["objectives"]
        branch = "" if depth == 0 else ("└─ " if last else "├─ ")
        badge = _c(f"[{TAG.get(st, '?')}]", st, color)
        if is_obj:
            sc = snap.scores.get(nid)
            line = f"{badge} {_c(n['name'], 'bold', color)}  score {fmt(sc) if sc is not None else 'n/a'}"
        else:
            unit = n.get("unit", "")
            line = f"{badge} {n['name']}  = {fmt(snap.values.get(nid))} {unit}  ({_target_text(n)})  [{nid}]"
        out.append(prefix + branch + line)
        cs = [c for c in kids.get(nid, []) if visible(c)]
        for i, c in enumerate(cs):
            walk(c, prefix + ("" if depth == 0 else ("   " if last else "│  ")), i == len(cs) - 1, depth + 1)

    for r in _roots(m):
        walk(r, "", True, 0)
    return "\n".join(out)


def text_gates(m, snap, sched, color=True):
    out = [_c("PROCESS FLOW (unit-level sequential gates)", "bold", color)]
    for gid, r in snap.process_gates.items():
        g = m["process_gates"][gid]
        stage = m["stages"][g["stage"]]["name"]
        out.append(f"  {stage:<18} -> {_c(f"{r['status'].upper():<8}", r['status'], color)} {g['name']}  readiness {r['readiness']:.0%}")
        for c in r["criteria"]:
            mark = "ok " if c["ok"] else ("XX " if c["hard"] else "-- ")
            out.append(f"       {mark}{'HARD' if c['hard'] else 'soft'}  {c['text']}")
    out.append("")
    out.append(_c("PROGRAM GATES (pilot phases: business + technical delivery)", "bold", color))
    for gid, r in snap.program_gates.items():
        g = m["program_gates"][gid]
        slip = r.get("slip_wks")
        when = f"projected wk {r['projected_week']:.1f} vs plan wk {r.get('planned_week')}" + (
            f"  ({'+' if slip > 0 else ''}{slip:.1f} wks)" if slip is not None else "")
        out.append(f"  {_c(f"{r['status'].upper():<8}", r['status'], color)} {g['name']:<44} readiness {r['readiness']:.0%}   {when}")
        for c in r["criteria"]:
            if not c["ok"]:
                out.append(f"       {'XX HARD' if c['hard'] else '-- soft'}  {c['text']}")
    out.append("")
    if m["constraints"]:
        out.append(_c("CONSTRAINTS", "bold", color))
        for cid, ok in snap.constraints.items():
            c = m["constraints"][cid]
            st = "pass" if ok else ("blocked" if ok is None else "fail")
            label = "HOLDS" if ok else ("N/A" if ok is None else "VIOLATED")
            out.append(f"  {_c(f'{label:<9}', st, color)}{'HARD' if c.get('hard', True) else 'soft'}  "
                       f"{c.get('kind', ''):<15}{c['name']}   [{c['expr']}]")
        out.append("")
    out.append(_c("CRITICAL PATH (remaining work)", "bold", color))
    out.append("  " + " -> ".join(f"{d} ({sched['finish'][d]:.1f}w)" for d in sched["critical_path"]))
    return "\n".join(out)


def text_prediction(m, snap, mc, color=True):
    out = [_c(f"PREDICTION  Monte Carlo {mc['runs']} runs over {len(mc['stochastic_vars'])} uncertain inputs", "bold", color)]
    out.append(f"  {'KPI':<44}{'now':>12}{'P10':>12}{'P50':>12}{'P90':>12}{'P(green)':>10}")
    for kid, r in mc["kpis"].items():
        k = m["kpis"][kid]
        if k.get("target") is None:
            continue
        # only show KPIs that actually vary
        if r["p10"] == r["p90"]:
            continue
        out.append(f"  {k['name'][:43]:<44}{fmt(snap.values[kid]):>12}{fmt(r['p10']):>12}{fmt(r['p50']):>12}{fmt(r['p90']):>12}{r['p_green']:>10.0%}")
    out.append("")
    out.append("  Gate pass probability:")
    for g, p in mc["gate_pass_prob"].items():
        name = (m["process_gates"].get(g) or m["program_gates"].get(g))["name"]
        out.append(f"    {name:<46}{p:>6.0%}")
        for k, f in list(mc["gate_fail_reasons"][g].items())[:4]:
            out.append(f"        fails on {k:<36}{f:>6.1%} of runs")
    if mc.get("constraint_hold_prob"):
        out.append("  Constraint hold probability:")
        for c, p in mc["constraint_hold_prob"].items():
            out.append(f"    {m['constraints'][c]['name']:<46}{p:>6.0%}")
    out.append("  Bottleneck stage probability: " + ", ".join(
        f"{(m['kpis'].get(b) or {}).get('name', b)} {p:.0%}" for b, p in sorted(mc["bottleneck_prob"].items(), key=lambda x: -x[1])))
    return "\n".join(out)


# ------------------------------------------------------------------ mermaid
def _mid(s):
    return re.sub(r"\W", "_", s)


def mermaid(m, snap, sched):
    kids = children_index(m)
    L = ["## KPI tree", "", "```mermaid", "flowchart LR"]
    for sec in ("objectives", "kpis"):
        for nid, n in m[sec].items():
            label = n["name"].replace('"', "'")
            if sec == "kpis" and snap.values.get(nid) is not None:
                label += f"<br/>{fmt(snap.values[nid])} {n.get('unit', '')}"
            shape = ('(["', '"])') if sec == "objectives" else ('["', '"]')
            L.append(f"  {_mid(nid)}{shape[0]}{label}{shape[1]}:::{snap.status.get(nid, 'no_data')}")
    for p, cs in kids.items():
        for c in cs:
            L.append(f"  {_mid(p)} --> {_mid(c)}")
    L += ["  classDef green fill:#d7f5dd,stroke:#1a7f37", "  classDef amber fill:#fff1c7,stroke:#9a6700",
          "  classDef red fill:#ffd8d3,stroke:#cf222e", "  classDef no_data fill:#eee,stroke:#999",
          "  classDef info fill:#ddf4ff,stroke:#0969da", "```", ""]

    L += ["## Sequential gated process flow", "", "```mermaid", "flowchart LR"]
    prev = None
    for gid, r in snap.process_gates.items():
        g = m["process_gates"][gid]
        st = g["stage"]
        L.append(f"  {st}[\"{m['stages'][st]['name']}\"] --> {gid}{{\"{g['name']}<br/>{r['status']}\"}}:::{r['status']}")
        if prev:
            L.append(f"  {prev} --> {st}")
        prev = gid
    L += ["  classDef pass fill:#d7f5dd,stroke:#1a7f37", "  classDef fail fill:#ffd8d3,stroke:#cf222e",
          "  classDef blocked fill:#eee,stroke:#999", "```", ""]

    L += ["## Business & technical delivery dependencies", "", "```mermaid", "flowchart LR"]
    crit = set(sched["critical_path"])
    for d, x in m["deliverables"].items():
        cls = "crit" if d in crit else x.get("type", "technical")
        L.append(f"  {d}[\"{x['name']}<br/>{x.get('pct_complete', 0):.0%}\"]:::{cls}")
        for p in x.get("depends_on") or []:
            L.append(f"  {p} --> {d}")
    for gid, g in m["program_gates"].items():
        L.append(f"  {gid}{{{{\"{g['name']}\"}}}}:::gate")
        for d in g.get("deliverables") or []:
            L.append(f"  {d} --> {gid}")
    L += ["  classDef business fill:#ddf4ff,stroke:#0969da", "  classDef technical fill:#fbefff,stroke:#8250df",
          "  classDef operational fill:#fff1c7,stroke:#9a6700", "  classDef crit fill:#ffd8d3,stroke:#cf222e,stroke-width:3px",
          "  classDef gate fill:#222,color:#fff", "```"]
    return "\n".join(L)


# -------------------------------------------------------------------- turtle
def turtle(m, base="https://example.org/mfg-kpi#"):
    def lit(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(str(v))

    cls = {"domains": "Domain", "stages": "ProcessStage", "objectives": "Objective", "kpis": "KPI",
           "variables": "Variable", "process_gates": "ProcessGate", "program_gates": "ProgramGate",
           "deliverables": "Deliverable", "constraints": "Constraint"}
    L = [f"@prefix mfg: <{base}> .", "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
         "@prefix owl: <http://www.w3.org/2002/07/owl#> .", ""]
    for c in cls.values():
        L.append(f"mfg:{c} a owl:Class .")
    for p in ("parent", "inDomain", "inStage", "computedFrom", "dependsOn", "requires", "gatesStage", "requiresGate", "protectsObjective"):
        L.append(f"mfg:{p} a owl:ObjectProperty .")
    L.append("")
    refs = {"parent": "parent", "domain": "inDomain", "stage": "inStage", "objective": "protectsObjective"}
    skip = {"id", "name", "criteria", "deliverables", "depends_on", "dist", "attributes", "_source"} | set(refs)
    for sec, c in cls.items():
        for nid, n in m[sec].items():
            props = [f"a mfg:{c}", f"rdfs:label {lit(n.get('name', nid))}"]
            for k, p in refs.items():
                if n.get(k):
                    props.append(f"mfg:{p} mfg:{n[k]}")
            if sec == "constraints" and n.get("expr"):
                props.append("mfg:computedFrom " + ", ".join(f"mfg:{x}" for x in sorted(formula_names(n["expr"]))))
            if sec == "kpis" and n.get("formula"):
                props.append("mfg:computedFrom " + ", ".join(f"mfg:{x}" for x in sorted(formula_names(n["formula"]))))
            for d in n.get("depends_on") or []:
                props.append(f"mfg:dependsOn mfg:{d}")
            for d in n.get("deliverables") or []:
                props.append(f"mfg:requires mfg:{d}")
            for cr in n.get("criteria") or []:
                if "kpi" in cr:
                    props.append(f"mfg:requires mfg:{cr['kpi']}")
                if "gate" in cr:
                    props.append(f"mfg:requiresGate mfg:{cr['gate']}")
                if "constraint" in cr:
                    props.append(f"mfg:requires mfg:{cr['constraint']}")
            if sec == "process_gates":
                props.append(f"mfg:gatesStage mfg:{n['stage']}")
            for k, v in n.items():
                if k not in skip and isinstance(v, (str, int, float, bool)):
                    props.append(f"mfg:{_mid(k)} {lit(v)}")
            for k, v in (n.get("attributes") or {}).items():
                props.append(f"mfg:{_mid(k)} {lit(v)}")
            L.append(f"mfg:{nid} " + " ;\n    ".join(props) + " .")
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------- csv
def kpi_dictionary(m, snap, path):
    def lineage(nid):
        chain, cur = [], node(m, nid).get("parent")
        while cur:
            chain.append(cur)
            cur = node(m, cur).get("parent")
        return " > ".join(reversed(chain))

    cols = ["id", "name", "lineage", "domain", "stage", "formula", "unit", "direction", "target", "threshold",
            "weight", "kind", "mode", "frequency", "owner", "value", "status", "score"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for kid, k in m["kpis"].items():
            w.writerow([kid, k.get("name"), lineage(kid), k.get("domain"), k.get("stage"), k.get("formula"),
                        k.get("unit"), k.get("direction", "up" if k.get("target") is not None else ""),
                        k.get("target"), k.get("threshold"), k.get("weight", 1), k.get("kind"), k.get("mode"),
                        k.get("frequency"), k.get("owner"), snap.values.get(kid), snap.status.get(kid),
                        snap.scores.get(kid)])


def snapshot_json(m, snap, sched, mc=None):
    return json.dumps({
        "kpis": {k: {"value": snap.values.get(k), "status": snap.status.get(k), "score": snap.scores.get(k)} for k in m["kpis"]},
        "objectives": {o: {"score": snap.scores.get(o), "status": snap.status.get(o)} for o in m["objectives"]},
        "constraints": snap.constraints, "process_gates": snap.process_gates, "program_gates": snap.program_gates,
        "bottleneck": snap.bottleneck, "schedule": sched, "monte_carlo": mc,
    }, indent=2, default=str)


# ---------------------------------------------------------------------- html
_CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e4e4e7;
--g:#1a7f37;--gb:#e3f6e8;--a:#9a6700;--ab:#fff4d1;--r:#cf222e;--rb:#ffe5e2;--n:#8b8b90;--nb:#f0f0f1}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141416;--fg:#ececef;--muted:#9a9aa0;
--card:#1d1d20;--line:#2e2e33;--g:#4ac26b;--gb:#16301f;--a:#e3b341;--ab:#33290f;--r:#ff7b72;--rb:#3a1a1a;--n:#8b8b90;--nb:#26262a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 10px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}.tile .l{font-size:12px;color:var(--muted)}
.b{display:inline-block;min-width:54px;text-align:center;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600;text-transform:uppercase}
.green,.pass{color:var(--g);background:var(--gb)}.amber{color:var(--a);background:var(--ab)}.red,.fail{color:var(--r);background:var(--rb)}
.no_data,.info,.blocked{color:var(--n);background:var(--nb)}
.flow{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}.flow .card{min-width:190px;flex:1}
details{margin-left:14px;border-left:1px solid var(--line);padding-left:10px}details>summary{cursor:pointer;padding:3px 0;list-style:none}
details>summary::-webkit-details-marker{display:none}summary::before{content:"▸ ";color:var(--muted)}details[open]>summary::before{content:"▾ "}
.kpi{margin-left:14px;padding:3px 0 3px 10px;border-left:1px solid var(--line);display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.num{font-variant-numeric:tabular-nums;font-weight:600}.id{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}
.crit td:first-child{border-left:3px solid var(--r)}ul.cr{margin:6px 0 0;padding-left:16px;font-size:12px}
"""


def html_report(m, snap, sched, mc=None, title="Pilot Line KPI Tree"):
    e = html.escape
    kids = children_index(m)

    def badge(st):
        return f'<span class="b {e(st)}">{e(st.replace("_", " "))}</span>'

    def tree(nid, open_=True):
        n = node(m, nid)
        st = snap.status.get(nid, "no_data")
        cs = kids.get(nid, [])
        if nid in m["kpis"]:
            body = (f'{badge(st)} {e(n["name"])} <span class="num">{e(fmt(snap.values.get(nid)))}</span> '
                    f'<span class="muted">{e(n.get("unit", ""))} &nbsp; {e(_target_text(n))}</span> <span class="id">{e(nid)}</span>')
            if not cs:
                return f'<div class="kpi">{body}</div>'
            return f'<details><summary>{body}</summary>{"".join(tree(c, False) for c in cs)}</details>'
        sc = snap.scores.get(nid)
        head = f'{badge(st)} <b>{e(n["name"])}</b> <span class="muted">score {fmt(sc) if sc is not None else "n/a"}</span>'
        return f'<details{" open" if open_ else ""}><summary>{head}</summary>{"".join(tree(c, False) for c in cs)}</details>'

    root = _roots(m)[0]
    tiles = [("Pilot line health", fmt(snap.scores.get(root)), snap.status.get(root))]
    for kid in ("line_capacity_uph", "line_rty", "pred_demand_coverage", "auto_labor_displacement", "biz_payback_months"):
        if kid in m["kpis"]:
            tiles.append((m["kpis"][kid]["name"], f'{fmt(snap.values.get(kid))} {m["kpis"][kid].get("unit", "")}', snap.status.get(kid)))
    tiles_html = "".join(f'<div class="card tile"><div class="l">{e(l)}</div><div class="v">{e(v)}</div>{badge(st)}</div>'
                         for l, v, st in tiles)

    def gate_card(name, sub, r):
        crit = "".join(f'<li>{"✔" if c["ok"] else "✘"} {"" if c["hard"] else "(soft) "}{e(c["text"])}</li>' for c in r["criteria"])
        return (f'<div class="card"><div class="muted">{e(sub)}</div><b>{e(name)}</b><div>{badge(r["status"])} '
                f'readiness {r["readiness"]:.0%}</div><ul class="cr">{crit}</ul></div>')

    flow = "".join(gate_card(m["process_gates"][g]["name"], m["stages"][m["process_gates"][g]["stage"]]["name"], r)
                   for g, r in snap.process_gates.items())
    prog = "".join(gate_card(m["program_gates"][g]["name"],
                             f'proj. wk {r.get("projected_week", 0):.1f} / plan wk {r.get("planned_week")}', r)
                   for g, r in snap.program_gates.items())

    crit = set(sched["critical_path"])
    drows = "".join(
        f'<tr class="{"crit" if d in crit else ""}"><td>{e(x["name"])} <span class="id">{e(d)}</span></td><td>{e(x.get("type", ""))}</td>'
        f'<td class="r">{x.get("pct_complete", 0):.0%}</td><td class="r">{sched["finish"][d]:.1f}</td>'
        f'<td>{e(", ".join(x.get("depends_on") or []))}</td></tr>' for d, x in m["deliverables"].items())

    mc_html = ""
    if mc:
        rows = "".join(
            f'<tr><td>{e(m["kpis"][k]["name"])}</td><td class="r">{e(fmt(snap.values[k]))}</td><td class="r">{e(fmt(r["p10"]))}</td>'
            f'<td class="r">{e(fmt(r["p50"]))}</td><td class="r">{e(fmt(r["p90"]))}</td><td class="r">{r["p_green"]:.0%}</td></tr>'
            for k, r in mc["kpis"].items() if m["kpis"][k].get("target") is not None and r["p10"] != r["p90"])
        grows = "".join(f'<tr><td>{e((m["process_gates"].get(g) or m["program_gates"].get(g))["name"])}</td><td class="r">{p:.0%}</td>'
                        f'<td class="muted">{e(", ".join(f"{k} {f:.0%}" for k, f in list(mc["gate_fail_reasons"][g].items())[:3]))}</td></tr>'
                        for g, p in mc["gate_pass_prob"].items())
        mc_html = (f'<h2>Prediction — Monte Carlo ({mc["runs"]} runs, {len(mc["stochastic_vars"])} uncertain inputs)</h2>'
                   f'<div class="card scroll"><table><tr><th>KPI</th><th class="r">Now</th><th class="r">P10</th><th class="r">P50</th>'
                   f'<th class="r">P90</th><th class="r">P(green)</th></tr>{rows}</table></div>'
                   f'<h2>Gate pass probability</h2><div class="card scroll"><table><tr><th>Gate</th><th class="r">P(pass)</th>'
                   f'<th>Main reasons it fails (share of runs)</th></tr>{grows}</table></div>')

    cons_html = ""
    if m["constraints"]:
        crow = "".join(
            f'<tr><td>{badge("pass" if ok else ("blocked" if ok is None else "fail"))}</td><td>{e(m["constraints"][c]["name"])}</td>'
            f'<td>{e(m["constraints"][c].get("kind", ""))}</td><td>{"hard" if m["constraints"][c].get("hard", True) else "soft"}</td>'
            f'<td class="id">{e(m["constraints"][c]["expr"])}</td>'
            f'<td class="r">{format(mc["constraint_hold_prob"][c], ".0%") if mc and c in mc["constraint_hold_prob"] else ""}</td></tr>'
            for c, ok in snap.constraints.items())
        cons_html = (f'<h2>Constraints</h2><div class="card scroll"><table><tr><th></th><th>Constraint</th><th>Kind</th><th></th>'
                     f'<th>Rule</th><th class="r">P(holds)</th></tr>{crow}</table></div>')
    bn = m["kpis"].get(snap.bottleneck, {}).get("name", "n/a")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{_CSS}</style></head><body><main>
<h1>{e(m["objectives"][root]["name"])}</h1>
<div class="muted">KPI tree, sequential gates, delivery dependencies and prediction. Current bottleneck: <b>{e(bn)}</b></div>
<h2>Headline</h2><div class="tiles">{tiles_html}</div>
<h2>Sequential gated process flow</h2><div class="flow">{flow}</div>
<h2>Program gates (business + technical delivery)</h2><div class="flow">{prog}</div>
<h2>KPI tree</h2><div class="card">{tree(root)}</div>
<h2>Delivery dependencies &amp; critical path</h2><div class="card scroll"><table><tr><th>Deliverable</th><th>Type</th>
<th class="r">Done</th><th class="r">Finish (wks)</th><th>Depends on</th></tr>{drows}</table>
<div class="muted" style="margin-top:6px">Red edge = critical path: {e(" → ".join(sched["critical_path"]))}</div></div>
{cons_html}
{mc_html}
</main></body></html>"""
