"""Turn the variables into a questionnaire for manual extraction: a Markdown
document to send, and a CSV to fill in that `--values` reads straight back."""
from __future__ import annotations

import csv
from collections import defaultdict

from .model import formula_names

WHO = {"CM": "Contract manufacturer", "quality": "Quality", "eng": "Engineering", "finance": "Finance",
       "pmo": "Program office", "ehs": "EHS / Safety", "it_ot": "IT / OT", "plan": "Planning"}


def used_by(m):
    idx = defaultdict(list)
    for kid, k in m["kpis"].items():
        if k.get("formula"):
            for n in formula_names(k["formula"]):
                idx[n].append(kid)
    return idx


def _question(v):
    if v.get("question"):
        return v["question"]
    what = v.get("description") or v["id"].replace("_", " ")
    return f"What was the value of '{what}' this week" + (f" (in {v['unit']})?" if v.get("unit") else "?")


def questionnaire(m, only=None, period=None):
    ids = [v for v in m["variables"] if only is None or v in only]
    uses = used_by(m)
    groups = defaultdict(list)
    for vid in ids:
        groups[m["variables"][vid].get("ask", "unassigned")].append(vid)

    title = f"Weekly KPI data request{' - ' + period if period else ''}"
    md = [f"# {title}", "",
          "Answer for the reporting week unless the question says otherwise. If a number is an estimate, say so in the comment.",
          "Fill the `value` column of the matching CSV and run `python -m kpi_ontology report --values <file>.csv`.", ""]
    rows = []
    n = 0
    for who in sorted(groups, key=lambda w: (w != "CM", w)):
        md += [f"## {WHO.get(who, who)}", ""]
        for vid in groups[who]:
            v = m["variables"][vid]
            n += 1
            q = _question(v)
            md.append(f"{n}. **{q}**  ")
            meta = [f"`{vid}`"]
            if v.get("unit"):
                meta.append(f"unit: {v['unit']}")
            if uses.get(vid):
                meta.append("feeds: " + ", ".join(m["kpis"][k]["name"] for k in uses[vid][:3]))
            md.append("   " + " · ".join(meta) + "  ")
            if v.get("clarify"):
                md.append(f"   _Definition to agree:_ {v['clarify']}")
            md.append("")
            rows.append({"id": vid, "ask": who, "question": q, "unit": v.get("unit", ""), "value": "", "comment": "",
                         "clarify": v.get("clarify", ""), "baseline_value": v.get("value", "")})
    return "\n".join(md), rows


def write_questionnaire(m, md_path, csv_path, only=None, period=None):
    md, rows = questionnaire(m, only, period)
    with open(md_path, "w") as fh:
        fh.write(md)
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "ask", "question", "unit", "value", "comment", "clarify", "baseline_value"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)
