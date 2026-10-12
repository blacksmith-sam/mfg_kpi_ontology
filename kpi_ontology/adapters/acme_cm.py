"""Example CM-specific adapter. Copy this file for your own contract manufacturer.

Most values come from the mapping YAML (mappings/acme_cm_weekly.yaml). This
class adds only the values that need logic the mapping cannot express.
"""
from __future__ import annotations

from .base import Extracted, ReportAdapter, register

AUTOMATED_STAGES = {"build", "pack"}


@register
class AcmeCMWeeklyAdapter(ReportAdapter):
    name = "acme_cm_weekly"

    def custom_fields(self, doc, values):
        out = {}
        log = self._downtime_log(doc)
        if log is None:
            return out
        rows, where = log

        # Human interventions: every "Operator assist" event on an automated stage
        assists = [r for r in rows if r["category"] == "operator assist" and r["stage"] in AUTOMATED_STAGES]
        out["human_interventions"] = Extracted(float(len(assists)), f"{len(assists)} rows", where,
                                               "custom: count Operator assist on automated stages")

        # Robot failures and repair time: "Breakdown" events on automated stages
        brk = [r for r in rows if r["category"] == "breakdown" and r["stage"] in AUTOMATED_STAGES]
        if brk:
            out["robot_failures"] = Extracted(float(len(brk)), f"{len(brk)} rows", where, "custom: count Breakdown")
            out["robot_repair_hrs"] = Extracted(sum(r["minutes"] for r in brk) / 60, "sum minutes / 60", where,
                                                "custom: sum Breakdown minutes")

        # Automated cycles = units started at automated stages
        if "asm_units_started" in values and "pkg_units" in values:
            out["auto_cycles"] = Extracted(values["asm_units_started"] + values["pkg_units"], "build + pack units",
                                           "derived", "custom: asm_units_started + pkg_units")
        return out

    def _downtime_log(self, doc):
        for s in doc.sheet("Downtime"):
            hdr = self._find_header(s, ["Stage", "Minutes", "Category"])
            if hdr is None:
                continue
            hi, cols = hdr
            rows = []
            for row in s.rows[hi + 1:]:
                try:
                    rows.append({"stage": str(row[cols["Stage"]]).strip().lower(),
                                 "minutes": float(row[cols["Minutes"]]),
                                 "category": str(row[cols["Category"]]).strip().lower()})
                except (TypeError, ValueError, IndexError):
                    continue  # blank or total rows
            return rows, f"{s.name} (downtime log, {len(rows)} rows)"
        return None
