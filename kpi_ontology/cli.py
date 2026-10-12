"""Command line: python -m kpi_ontology <command> [options]

Model & evaluation
  validate   check ids, references, formulas, cycles
  tree       KPI tree with values and RAG status   (--domain to filter)
  gates      process gates, program gates, constraints, critical path
  predict    Monte Carlo prediction, gate pass probability and why gates fail
  export     write out/: report.html, kpi_tree.md, ontology.ttl, kpi_dictionary.csv, snapshot.json
  report     all of the above (default)

Weekly contract-manufacturer data
  extract    read CM report(s) with a mapping/adapter -> data/runs/<period>.yaml
  questions  questionnaire for manual extraction (all variables, or the gaps of one run)
  history    week-over-week KPI trend, SPC flags, gate results, constraint violations
  propose    draft constraints, objectives, KPIs, targets and distributions from the runs

Examples
  python -m kpi_ontology extract --input data/inbox
  python -m kpi_ontology report --run data/runs/2026-W41.yaml
  python -m kpi_ontology history
  python -m kpi_ontology propose
  python -m kpi_ontology questions --run data/runs/2026-W41.yaml
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

from . import engine, export
from .model import load_model, load_values, validate

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = ["validate", "tree", "gates", "predict", "export", "report", "extract", "questions", "history", "propose"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kpi_ontology", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="report", choices=COMMANDS)
    ap.add_argument("--model", nargs="+", default=[os.path.join(HERE, "model"), os.path.join(HERE, "extensions")],
                    help="model dirs/files, merged in order (default: model/ then extensions/)")
    ap.add_argument("--add", action="append", default=[], help="extra model file merged last (e.g. a proposal to try)")
    ap.add_argument("--values", "--run", action="append", default=[], dest="values",
                    help="measured values: run file from `extract`, .yaml or .csv (id,value); repeatable")
    ap.add_argument("--set", action="append", default=[], metavar="VAR=VALUE", help="override one variable")
    ap.add_argument("--domain", help="tree: only show nodes in this domain")
    ap.add_argument("--runs", type=int, default=2000, help="Monte Carlo runs")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--mapping", default=os.path.join(HERE, "mappings", "acme_cm_weekly.yaml"), help="extract: mapping YAML")
    ap.add_argument("--input", nargs="+", help="extract: report files or folders")
    ap.add_argument("--period", help="extract: force the period id (else read from the report / file name)")
    ap.add_argument("--runs-dir", default=os.path.join(HERE, "data", "runs"))
    ap.add_argument("--last", type=int, default=8, help="history: number of runs to show")
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args(argv)
    color = sys.stdout.isatty() and not a.no_color

    m = load_model(a.model + a.add)
    errors, warnings = validate(m)
    if a.command == "validate" or errors:
        print(f"Loaded {len(m['_files'])} files: {len(m['objectives'])} objectives, {len(m['kpis'])} KPIs, "
              f"{len(m['variables'])} variables, {len(m['constraints'])} constraints, {len(m['process_gates'])} process gates, "
              f"{len(m['program_gates'])} program gates, {len(m['deliverables'])} deliverables")
        for w in warnings:
            print("  warning:", w)
        for er in errors:
            print("  ERROR:", er)
        if errors:
            return 1
        print("  model OK")
        return 0

    if a.command == "extract":
        return _extract(m, a)
    if a.command == "questions":
        return _questions(m, a)
    if a.command in ("history", "propose"):
        return _history(m, a, color)

    values = {}
    for f in a.values:
        values.update(load_values(f, m))
    for s in a.set:
        k, v = s.split("=", 1)
        values[k.strip()] = float(v)
    unknown = [k for k in values if k not in m["variables"]]
    if unknown:
        print("warning: values for unknown variables ignored:", ", ".join(unknown), file=sys.stderr)
    if values:
        baseline = [v for v in m["variables"] if v not in values]
        print(f"Using {len(values) - len(unknown)} measured values; {len(baseline)} variables fall back to model baseline "
              f"values{': ' + ', '.join(baseline[:12]) + (' ...' if len(baseline) > 12 else '') if baseline else ''}\n")

    sched = engine.schedule(m)
    snap = engine.evaluate(m, values, sched)
    mc = engine.monte_carlo(m, a.runs, a.seed, values) if a.command in ("predict", "export", "report") else None

    if a.command in ("tree", "report"):
        print(export.text_tree(m, snap, color, a.domain), "\n")
    if a.command in ("gates", "report"):
        print(export.text_gates(m, snap, sched, color), "\n")
    if a.command in ("predict", "report"):
        print(export.text_prediction(m, snap, mc, color), "\n")
    if a.command in ("export", "report"):
        os.makedirs(a.out, exist_ok=True)
        files = {
            "report.html": export.html_report(m, snap, sched, mc),
            "kpi_tree.md": export.mermaid(m, snap, sched),
            "ontology.ttl": export.turtle(m),
            "snapshot.json": export.snapshot_json(m, snap, sched, mc),
        }
        for name, text in files.items():
            with open(os.path.join(a.out, name), "w") as fh:
                fh.write(text)
        export.kpi_dictionary(m, snap, os.path.join(a.out, "kpi_dictionary.csv"))
        print("wrote", ", ".join(sorted(list(files) + ["kpi_dictionary.csv"])), "to", a.out)
    return 0


def _extract(m, a):
    from .adapters import ReportAdapter
    if not a.input:
        print("extract needs --input <file or folder>", file=sys.stderr)
        return 2
    files = []
    for p in a.input:
        files += sorted(f for f in glob.glob(os.path.join(p, "*")) if os.path.isfile(f)) if os.path.isdir(p) else [p]
    adapter = ReportAdapter.from_mapping_file(a.mapping, m)
    print(f"adapter {adapter.name!r} with mapping {os.path.relpath(a.mapping)}")
    status, seen = 0, set()
    for f in files:
        try:
            res = adapter.extract(f, a.period)
        except ValueError as e:
            print(f"  skip {os.path.basename(f)}: {e}")
            continue
        period = res.period or os.path.splitext(os.path.basename(f))[0]
        out = os.path.join(a.runs_dir, f"{period}.yaml")
        if period in seen:  # a second report for the same week: keep it beside, not over, the first
            out = os.path.join(a.runs_dir, f"{period}__{os.path.splitext(os.path.basename(f))[0]}.yaml.dup")
        seen.add(period)
        res.to_run_file(out)
        print(f"  {os.path.basename(f)} -> {os.path.relpath(out)}  period {period}: {len(res.values)} values, "
              f"{len(res.commitments)} commitments, {len(res.extra)} extra fields")
        for label, items in (("NOT FOUND (mapped but not located)", res.not_found), ("checks failed", res.check_failures),
                             ("errors", res.errors)):
            if items:
                status = 1
                print(f"     {label}: {', '.join(items)}")
        if res.not_in_report:
            print(f"     {len(res.not_in_report)} model variables are not in this report (baseline used) -> "
                  f"python -m kpi_ontology questions --run {os.path.relpath(out)}")
    return status


def _questions(m, a):
    from .questions import write_questionnaire
    only, period, suffix = None, None, "all"
    if a.values:
        import yaml
        with open(a.values[0]) as fh:
            run = yaml.safe_load(fh)
        only = set(run.get("not_in_report") or []) | set(run.get("not_found") or [])
        period, suffix = run.get("period"), f"gaps_{run.get('period')}"
    os.makedirs(a.out, exist_ok=True)
    md, csv_path = os.path.join(a.out, f"questionnaire_{suffix}.md"), os.path.join(a.out, f"questionnaire_{suffix}.csv")
    n = write_questionnaire(m, md, csv_path, only, period)
    print(f"{n} questions -> {os.path.relpath(md)} (to send) and {os.path.relpath(csv_path)} (fill `value`, then "
          f"`report --values {os.path.relpath(csv_path)}`)")
    return 0


def _history(m, a, color):
    from . import history
    runs = history.load_runs(a.runs_dir)
    if not runs:
        print(f"no run files in {a.runs_dir}; run `extract` first", file=sys.stderr)
        return 2
    snaps = history.evaluate_runs(m, runs)
    if a.command == "history":
        print(history.text_history(m, runs, snaps, color, a.last))
        return 0
    doc, notes = history.propose(m, runs, snaps)
    doc = _drop_none(doc)
    path = os.path.join(HERE, "proposals", f"proposed_{runs[-1]['period']}.yaml")
    history.write_proposal(doc, notes, runs[-1]["period"], path)
    print(f"proposal from {len(runs)} runs -> {os.path.relpath(path)}")
    for sec, items in doc.items():
        print(f"  {sec:<12}{len(items):>3}  " + ", ".join(i["id"] for i in items[:6]) + (" ..." if len(items) > 6 else ""))
    for n in notes:
        print("  note:", n)
    print(f"  try it:  python -m kpi_ontology report --add {os.path.relpath(path)}")
    return 0


def _drop_none(x):
    if isinstance(x, dict):
        return {k: _drop_none(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [_drop_none(v) for v in x]
    return x
