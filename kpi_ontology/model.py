"""Load and validate the KPI ontology from YAML files.

The model is a dict of sections, each an ordered {id: node-dict}. Nodes stay
plain dicts so any custom field (or an `attributes:` map) survives into the
exports without code changes.
"""
from __future__ import annotations

import ast
import csv
import glob
import os
from collections import defaultdict

import yaml

NUMERIC_FIELDS = ("value", "target", "threshold", "weight", "duration_wks", "pct_complete", "planned_week")
SECTIONS = ("domains", "stages", "objectives", "kpis", "variables",
            "process_gates", "program_gates", "deliverables", "constraints")
FUNCS = {"min", "max", "abs", "round", "sqrt", "log", "exp", "clamp"}


def _yaml_files(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "*.yaml")) + glob.glob(os.path.join(p, "*.yml")))
        elif os.path.isfile(p):
            files.append(p)
    return files


def load_model(paths):
    """Merge every YAML file in `paths` (dirs or files) in order.

    A node whose id already exists is shallow-merged, so an extension file can
    override just one field (e.g. a target) of a core KPI.
    """
    m = {s: {} for s in SECTIONS}
    m["_files"] = []
    for f in _yaml_files(paths):
        with open(f) as fh:
            doc = yaml.safe_load(fh) or {}
        m["_files"].append(f)
        for sec in SECTIONS:
            for item in doc.get(sec) or []:
                item = dict(item)
                item.setdefault("_source", os.path.basename(f))
                for k in NUMERIC_FIELDS:  # YAML 1.1 reads "1e9" as a string
                    if isinstance(item.get(k), str):
                        try:
                            item[k] = float(item[k])
                        except ValueError:
                            pass
                old = m[sec].get(item["id"])
                m[sec][item["id"]] = {**old, **item, "_source": old["_source"]} if old else item
        for add in doc.get("gate_criteria_additions") or []:
            gate = m["process_gates"].get(add["gate"]) or m["program_gates"].get(add["gate"])
            if gate is None:
                raise KeyError(f"{f}: gate_criteria_additions refers to unknown gate {add['gate']!r}")
            gate["criteria"] = list(gate.get("criteria") or []) + list(add["criteria"])
    return m


def load_values(path, model=None):
    """Read measured values from YAML ({id: value} or variables list) or CSV (id,value).
    For run files, `extra` report fields are included when the model defines them."""
    if path.endswith(".csv"):
        with open(path, newline="") as fh:
            return {r["id"]: float(r["value"]) for r in csv.DictReader(fh) if r.get("value") not in (None, "")}
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    if isinstance(doc.get("values"), dict):  # run file written by `extract`
        vals = {k: v for k, v in doc["values"].items() if v is not None}
        if model is not None:
            vals.update({k: e["value"] for k, e in (doc.get("extra") or {}).items() if k in model["variables"]})
        return vals
    if "variables" in doc:
        return {v["id"]: v["value"] for v in doc["variables"]}
    return dict(doc)


def formula_names(expr):
    return {n.id for n in ast.walk(ast.parse(str(expr), mode="eval"))
            if isinstance(n, ast.Name) and n.id not in FUNCS}


def children_index(m):
    kids = defaultdict(list)
    for sec in ("objectives", "kpis"):
        for nid, n in m[sec].items():
            if n.get("parent"):
                kids[n["parent"]].append(nid)
    return kids


def node(m, nid):
    for sec in ("objectives", "kpis", "variables"):
        if nid in m[sec]:
            return m[sec][nid]
    return None


def topo_order(ids, deps):
    """Kahn topological sort. deps: id -> iterable of ids it depends on. Raises on cycles."""
    order, state = [], {}

    def visit(i, path):
        if state.get(i) == 2:
            return
        if state.get(i) == 1:
            raise ValueError("dependency cycle: " + " -> ".join(path + [i]))
        state[i] = 1
        for d in deps.get(i, ()):
            if d in ids:
                visit(d, path + [i])
        state[i] = 2
        order.append(i)

    for i in ids:
        visit(i, [])
    return order


def validate(m):
    """Return (errors, warnings)."""
    errors, warnings = [], []
    ns = defaultdict(list)
    for sec in ("objectives", "kpis", "variables"):
        for nid in m[sec]:
            ns[nid].append(sec)
    for nid, secs in ns.items():
        if len(secs) > 1:
            errors.append(f"id {nid!r} is used in several sections: {secs}")
        if not nid.isidentifier():
            errors.append(f"id {nid!r} must be a valid identifier (letters, digits, _) to be usable in formulas")

    for sec in SECTIONS:
        for nid, n in m[sec].items():
            stray = [k for k, v in n.items() if v is None]
            if stray:
                errors.append(f"{nid}: keys with no value {stray} - quote text that contains commas in {{...}} YAML")

    tree_ids = set(m["objectives"]) | set(m["kpis"])
    for sec in ("objectives", "kpis"):
        for nid, n in m[sec].items():
            if n.get("parent") and n["parent"] not in tree_ids:
                errors.append(f"{nid}: parent {n['parent']!r} does not exist")
    for sec in ("kpis", "variables", "stages"):
        for nid, n in m[sec].items():
            if n.get("domain") and n["domain"] not in m["domains"]:
                errors.append(f"{nid}: unknown domain {n['domain']!r}")
            if n.get("stage") and n["stage"] not in m["stages"]:
                errors.append(f"{nid}: unknown stage {n['stage']!r}")

    deps = {}
    for kid, k in m["kpis"].items():
        if not k.get("formula"):
            continue
        try:
            names = formula_names(k["formula"])
        except SyntaxError as e:
            errors.append(f"{kid}: bad formula {k['formula']!r}: {e.msg}")
            continue
        for nm in names:
            if nm not in m["variables"] and nm not in m["kpis"]:
                errors.append(f"{kid}: formula references unknown name {nm!r}")
        deps[kid] = {nm for nm in names if nm in m["kpis"]}
        t, th = k.get("target"), k.get("threshold")
        if t is not None and th is not None:
            up = k.get("direction", "up") == "up"
            if (up and th > t) or (not up and th < t):
                warnings.append(f"{kid}: threshold {th} is on the wrong side of target {t} for direction {k.get('direction', 'up')}")
    try:
        topo_order(list(m["kpis"]), deps)
    except ValueError as e:
        errors.append(f"KPI formulas: {e}")

    for cid, c in m["constraints"].items():
        try:
            for nm in formula_names(c["expr"]):
                if nm not in m["variables"] and nm not in m["kpis"]:
                    errors.append(f"constraint {cid}: unknown name {nm!r}")
        except (SyntaxError, KeyError) as e:
            errors.append(f"constraint {cid}: bad or missing expr ({e})")
        if c.get("objective") and c["objective"] not in m["objectives"]:
            errors.append(f"constraint {cid}: unknown objective {c['objective']!r}")

    gate_ids = set(m["process_gates"]) | set(m["program_gates"])
    for sec in ("process_gates", "program_gates"):
        for gid, g in m[sec].items():
            for c in g.get("criteria") or []:
                if "kpi" in c and c["kpi"] not in m["kpis"]:
                    errors.append(f"{gid}: criterion refers to unknown KPI {c['kpi']!r}")
                if "gate" in c and c["gate"] not in gate_ids:
                    errors.append(f"{gid}: criterion refers to unknown gate {c['gate']!r}")
                if "constraint" in c and c["constraint"] not in m["constraints"]:
                    errors.append(f"{gid}: criterion refers to unknown constraint {c['constraint']!r}")
            for d in g.get("deliverables") or []:
                if d not in m["deliverables"]:
                    errors.append(f"{gid}: unknown deliverable {d!r}")
    for gid, g in m["process_gates"].items():
        if g.get("stage") not in m["stages"]:
            errors.append(f"{gid}: unknown stage {g.get('stage')!r}")

    ddeps = {d: set(x.get("depends_on") or []) for d, x in m["deliverables"].items()}
    for d, ds in ddeps.items():
        for x in ds - set(m["deliverables"]):
            errors.append(f"{d}: depends on unknown deliverable {x!r}")
    try:
        topo_order(list(m["deliverables"]), ddeps)
    except ValueError as e:
        errors.append(f"deliverables: {e}")

    used = set()
    for c in m["constraints"].values():
        try:
            used |= formula_names(c.get("expr", ""))
        except SyntaxError:
            pass
    for k in m["kpis"].values():
        if k.get("formula"):
            try:
                used |= formula_names(k["formula"])
            except SyntaxError:
                pass
    for vid, v in m["variables"].items():
        if v.get("value") is None and not v.get("required_from_report"):
            warnings.append(f"variable {vid!r} has no baseline value (only defined in {v.get('_source')}?)")
    for v in m["variables"]:
        if v not in used:
            warnings.append(f"variable {v!r} is not used by any KPI")
    for kid, k in m["kpis"].items():
        if not k.get("formula") and not any(c.get("parent") == kid for c in m["kpis"].values()):
            warnings.append(f"KPI {kid!r} has no formula and no children")
    return errors, warnings
