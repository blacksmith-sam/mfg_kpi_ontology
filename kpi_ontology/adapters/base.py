"""Report adapters: pull variable values out of a contract manufacturer's
weekly report, with provenance for every number.

How it works
  1. `load()` turns the file into a neutral Document (sheets + text).
  2. Each entry under `fields:` in the mapping YAML is a *locator rule* that
     finds one value (see LOCATORS below). Rules may list `fallback:` rules
     for when the CM changes the layout.
  3. `custom_fields()` is the hook for values that need code, not just a lookup.
  4. `commitments:` pick up the targets the CM itself states in the report.
     `propose` turns them into constraints.
  5. `extra_fields:` collect numbers the report has but the model does not use
     yet. `propose` turns them into KPI candidates.
  6. Checks run, and the result is written as a run file (data/runs/<period>.yaml).

Locator rules (one per field):
  {cell: "Summary!C7"}                                  exact cell
  {label: "Units shipped", sheet: Summary}              value next to a label (to the right, or set offset: [down, right])
  {table: Production, row: "First pass good", col: Build}   row label x column header
  {table: Downtime, column: Minutes, agg: sum, where: {Stage: Build}}   aggregate a table column
  {regex: "Dock.to.stock[^0-9]*([0-9.]+)"}              regex over the whole text (group 1)
  {value: 40}                                           a constant
Common options: transform: "x * 60"  (may use names already extracted), percent: keep|ratio,
                required: true|false, fallback: [rule, ...]
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import re
from dataclasses import dataclass, field

import yaml

from ..engine import safe_eval
from .documents import Document, load_document


@dataclass
class Extracted:
    value: float
    raw: object
    where: str
    method: str


@dataclass
class ExtractionResult:
    source: str
    adapter: str
    period: str | None
    values: dict = field(default_factory=dict)       # var id -> number
    provenance: dict = field(default_factory=dict)   # var id -> Extracted
    commitments: dict = field(default_factory=dict)  # kpi/var id -> {op, value, where}
    extra: dict = field(default_factory=dict)        # report-only numbers -> value
    not_found: list = field(default_factory=list)    # mapped but not located
    not_in_report: list = field(default_factory=list)  # model variables the mapping does not cover
    check_failures: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_run_file(self, path):
        def prov(p):
            return {"raw": p.raw if isinstance(p.raw, (int, float, str)) else str(p.raw), "where": p.where, "method": p.method}
        doc = {
            "period": self.period, "source": os.path.basename(self.source), "adapter": self.adapter,
            "extracted_at": dt.datetime.now().isoformat(timespec="seconds"),
            "values": self.values, "commitments": self.commitments, "extra": self.extra,
            "not_found": self.not_found, "not_in_report": self.not_in_report,
            "check_failures": self.check_failures, "errors": self.errors,
            "provenance": {k: prov(v) for k, v in self.provenance.items()},
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True, width=120)


_NUM = re.compile(r"^\(?[-+]?[$€£]?\s*[-+]?(\d[\d,\s]*\.?\d*|\.\d+)(e[-+]?\d+)?\s*(%|k|m)?\)?$", re.I)


def parse_number(raw, percent="ratio"):
    """'1,234' -> 1234, '95.2%' -> 0.952 (or 95.2 when percent='keep'), '(12)' -> -12, '$4.2k' -> 4200."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, (dt.date, dt.datetime)):
        return None
    s = str(raw).strip()
    if not _NUM.match(s):
        m = re.search(r"[-+]?\d[\d,]*\.?\d*\s*%?", s)  # first number inside text, e.g. "approx 12 h"
        if not m:
            return None
        s = m.group(0)
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace("€", "").replace("£", "").replace(",", "").replace(" ", "")
    mult = 1.0
    if s[-1:] == "%":
        s, mult = s[:-1], (0.01 if percent == "ratio" else 1.0)
    elif s[-1:].lower() == "k":
        s, mult = s[:-1], 1e3
    elif s[-1:].lower() == "m":
        s, mult = s[:-1], 1e6
    try:
        v = round(float(s) * mult, 10)
    except ValueError:
        return None
    return -v if neg else v


def _match(cell, text, how="contains"):
    if cell is None:
        return False
    c, t = str(cell).strip().lower(), str(text).strip().lower()
    if how == "exact":
        return c == t
    if how == "regex":
        return re.search(text, str(cell), re.I) is not None
    return t in c


def _a1(r, c):
    col = ""
    c += 1
    while c:
        c, rem = divmod(c - 1, 26)
        col = chr(65 + rem) + col
    return f"{col}{r + 1}"


class ReportAdapter:
    """Generic, mapping-driven adapter. Subclass it for a specific CM when a
    value needs real logic: override `custom_fields` (and `load` for new formats)."""

    name = "generic"

    def __init__(self, mapping, model=None):
        self.mapping = mapping
        self.model = model

    @classmethod
    def from_mapping_file(cls, path, model=None):
        with open(path) as fh:
            mapping = yaml.safe_load(fh)
        adapter_cls = resolve_adapter(mapping.get("adapter", "generic"))
        return adapter_cls(mapping, model)

    # ------------------------------------------------------------ overridables
    def load(self, path) -> Document:
        return load_document(path)

    def custom_fields(self, doc: Document, values: dict) -> dict:
        """Return {var_id: Extracted} for values that need code. Default: none."""
        return {}

    # ---------------------------------------------------------------- locators
    def locate(self, doc, rule):
        for r in [rule] + list(rule.get("fallback") or []):
            hit = self._locate_one(doc, r)
            if hit is not None and hit.value is not None:
                return hit
        return None

    def _locate_one(self, doc, r):
        pct = r.get("percent", "ratio")
        if "value" in r:
            return Extracted(float(r["value"]), r["value"], "mapping constant", "value")
        if "cell" in r:
            sheet_name, ref = r["cell"].split("!")
            m = re.match(r"([A-Z]+)(\d+)", ref.upper())
            col = 0
            for ch in m.group(1):
                col = col * 26 + ord(ch) - 64
            for s in doc.sheet(sheet_name):
                raw = s.cell(int(m.group(2)) - 1, col - 1)
                return Extracted(parse_number(raw, pct), raw, f"{s.name}!{ref}", "cell")
        if "label" in r:
            dr, dc = r.get("offset", [0, None])
            for s in doc.sheet(r.get("sheet")):
                for i, row in enumerate(s.rows):
                    for j, c in enumerate(row):
                        if _match(c, r["label"], r.get("match", "contains")):
                            if dc is None:  # first numeric cell to the right
                                for k in range(j + 1, len(row)):
                                    v = parse_number(row[k], pct)
                                    if v is not None:
                                        return Extracted(v, row[k], f"{s.name}!{_a1(i, k)}", f"label '{r['label']}'")
                            else:
                                raw = s.cell(i + dr, j + dc)
                                return Extracted(parse_number(raw, pct), raw, f"{s.name}!{_a1(i + dr, j + dc)}", f"label '{r['label']}'")
        if "table" in r and "row" in r:
            for s in doc.sheet(r["table"]):
                hdr = self._find_header(s, [r["col"]])
                if hdr is None:
                    continue
                hi, cols = hdr
                for i in range(hi + 1, len(s.rows)):
                    if any(_match(c, r["row"], r.get("match", "contains")) for c in s.rows[i][:2]):
                        raw = s.cell(i, cols[r["col"]])
                        return Extracted(parse_number(raw, pct), raw, f"{s.name}!{_a1(i, cols[r['col']])}", "table row x col")
        if "table" in r and "column" in r:
            where = r.get("where") or {}
            for s in doc.sheet(r["table"]):
                hdr = self._find_header(s, [r["column"], *where])
                if hdr is None:
                    continue
                hi, cols = hdr
                vals = []
                for i in range(hi + 1, len(s.rows)):
                    row = s.rows[i]
                    if all(_match(row[cols[k]] if cols[k] < len(row) else None, v, "exact") for k, v in where.items()):
                        v = parse_number(row[cols[r["column"]]] if cols[r["column"]] < len(row) else None, pct)
                        if v is not None or r.get("agg") == "count":
                            vals.append(v)
                agg = r.get("agg", "sum")
                if not vals and agg != "count":
                    return None
                out = {"sum": lambda: sum(vals), "mean": lambda: sum(vals) / len(vals), "count": lambda: len(vals),
                       "max": lambda: max(vals), "min": lambda: min(vals), "last": lambda: vals[-1]}[agg]()
                return Extracted(float(out), f"{agg} of {len(vals)} rows", f"{s.name}[{r['column']}] where {where}", f"table {agg}")
        if "regex" in r:
            hits = re.findall(r["regex"], doc.text, re.I | re.M)
            nums = [parse_number(h if isinstance(h, str) else h[0], pct) for h in hits]
            nums = [n for n in nums if n is not None]
            if nums:
                v = sum(nums) if r.get("agg") == "sum" else nums[0]
                return Extracted(v, hits[0], f"text /{r['regex']}/", "regex")
        return None

    @staticmethod
    def _find_header(sheet, names):
        """Find the first row that contains every header in `names`; return (row index, {name: col})."""
        for i, row in enumerate(sheet.rows):
            cols = {}
            for n in names:
                for j, c in enumerate(row):
                    if _match(c, n, "exact"):
                        cols[n] = j
                        break
            if len(cols) == len(names):
                return i, cols
        return None

    # ------------------------------------------------------------------ period
    def find_period(self, doc, override=None):
        if override:
            return override
        rule = self.mapping.get("period")
        if rule:
            hit = None
            if "regex" in rule:
                m = re.search(rule["regex"], doc.text, re.I)
                hit = m.group(1) if m else None
            else:
                for s in doc.sheet(rule.get("sheet")):
                    for row in s.rows:
                        for j, c in enumerate(row):
                            if _match(c, rule["label"]) and j + 1 < len(row) and row[j + 1] is not None:
                                hit = row[j + 1]
                                break
                        if hit:
                            break
                    if hit:
                        break
            if isinstance(hit, (dt.date, dt.datetime)):
                y, w, _ = hit.isocalendar()
                return f"{y}-W{w:02d}"
            if hit:
                return str(hit).strip()
        m = re.search(r"(\d{4})[-_ ]?W(\d{1,2})", os.path.basename(doc.path), re.I)
        return f"{m.group(1)}-W{int(m.group(2)):02d}" if m else None

    # ----------------------------------------------------------------- extract
    def extract(self, path, period=None) -> ExtractionResult:
        doc = self.load(path)
        res = ExtractionResult(source=path, adapter=self.name, period=self.find_period(doc, period))
        fields = self.mapping.get("fields") or {}
        for vid, rule in fields.items():
            try:
                hit = self.locate(doc, rule)
            except Exception as e:  # keep going: one bad rule must not lose the whole report
                res.errors.append(f"{vid}: {type(e).__name__}: {e}")
                continue
            if hit is None:
                if rule.get("required", True):
                    res.not_found.append(vid)
                continue
            if rule.get("transform"):
                v = safe_eval(rule["transform"], {**res.values, "x": hit.value})
                if v is None:
                    res.errors.append(f"{vid}: transform {rule['transform']!r} failed on {hit.value}")
                    continue
                hit.method += f" -> {rule['transform']}"
                hit.value = v
            res.values[vid] = hit.value
            res.provenance[vid] = hit
        for vid, hit in (self.custom_fields(doc, dict(res.values)) or {}).items():
            res.values[vid], res.provenance[vid] = hit.value, hit
            if vid in res.not_found:
                res.not_found.remove(vid)

        for key, rule in (self.mapping.get("commitments") or {}).items():
            hit = self.locate(doc, rule)
            if hit is not None:
                res.commitments[key] = {"op": rule.get("op", ">="), "value": hit.value, "where": hit.where,
                                        "kind": rule.get("kind", "contract"), "hard": rule.get("hard", True)}
        for key, rule in (self.mapping.get("extra_fields") or {}).items():
            hit = self.locate(doc, rule)
            if hit is not None:
                entry = {"value": hit.value, "where": hit.where,
                         **{k: rule[k] for k in ("unit", "name", "direction") if k in rule}}
                tgt = self.locate(doc, rule["target_from"]) if rule.get("target_from") else None
                if tgt is not None:
                    entry["target"] = tgt.value
                res.extra[key] = entry

        if self.model is not None:
            res.not_in_report = [v for v in self.model["variables"] if v not in res.values]
            known = set(self.model["variables"])
            for vid in list(res.values):
                if vid not in known:
                    res.errors.append(f"{vid}: mapped field is not a model variable (typo?)")
        for chk in self.mapping.get("checks") or []:
            ok = safe_eval(chk, res.values)
            if ok is False:
                res.check_failures.append(chk)
        return res


ADAPTERS = {"generic": ReportAdapter}


def register(cls):
    """Class decorator: make an adapter selectable by `adapter: <name>` in a mapping."""
    ADAPTERS[cls.name] = cls
    return cls


def resolve_adapter(spec):
    """'generic', a registered name, or 'package.module:ClassName' for your own class."""
    if spec in ADAPTERS:
        return ADAPTERS[spec]
    if ":" in spec:
        mod, cls = spec.split(":")
        return getattr(importlib.import_module(mod), cls)
    raise KeyError(f"unknown adapter {spec!r}; registered: {', '.join(ADAPTERS)}")
