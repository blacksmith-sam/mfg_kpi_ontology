# Pilot Line KPI Tree / Ontology World Model

A YAML-defined, Python-evaluated KPI tree for an internal manufacturing pilot line
(**Receive & Kit → Build → Test → Package → Trace & Release**). It covers:

- **Objective → KPI → variable tree** with weighted roll-up and RAG status
- **Sequential gated process flow**: each stage has an exit gate. If a gate fails, every gate after it is **BLOCKED**
- **Program gates** (Design freeze → IQ → OQ → PQ → Ramp) that need business and technical deliverables **and** KPI criteria
- **Delivery dependency graph**: critical path, projected gate week vs plan
- **Prediction**: line capacity and bottleneck, rolled throughput yield (RTY), predicted pilot output, plus a Monte Carlo run that gives P10/P50/P90 values and gate pass probability
- **Automation displacement** KPIs (labor-hours, FTE, interventions per 1000 cycles, MTBF/MTTR) next to **human precision assembly** KPIs (Cpk, right-first-time fastening, poka-yoke coverage, certification, ergonomics, takt)
- Exports: HTML report, Mermaid diagrams, RDF/Turtle ontology, CSV KPI dictionary, JSON snapshot

Needs Python 3.10+ and PyYAML. Reading Excel/PDF reports also needs openpyxl/pypdf, which are installed in the project's `.venv`.

## Setup (once)

```bash
cd ~/projects/mfg_kpi_ontology
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate          # then `python` below means .venv/bin/python
```

## Run

```bash
python -m kpi_ontology validate                 # check references, formulas, cycles
python -m kpi_ontology tree                     # KPI tree with values
python -m kpi_ontology tree --domain assembly_manual
python -m kpi_ontology gates                    # process and program gates, critical path
python -m kpi_ontology predict --runs 5000      # Monte Carlo
python -m kpi_ontology report                   # everything, and writes out/
python -m kpi_ontology report --values data/example_week.csv --set asm_downtime_min=250
```

## Weekly contract-manufacturer workflow

```bash
python -m kpi_ontology extract --input data/inbox             # CM reports -> data/runs/<week>.yaml (values + provenance)
python -m kpi_ontology questions --run data/runs/2026-W41.yaml # questions for what the report did not contain
python -m kpi_ontology report --run data/runs/2026-W41.yaml --values out/questionnaire_gaps_2026-W41.csv
python -m kpi_ontology history                                # trend, SPC, gates and constraint violations per week
python -m kpi_ontology propose                                # draft constraints/objectives/KPIs/targets -> proposals/
python -m kpi_ontology report --add proposals/proposed_2026-W41.yaml   # try a proposal before adopting it
```

- **Adapter** = loader (Excel/CSV/PDF/text/JSON become one `Document`) + **mapping YAML** (`mappings/*.yaml`, one locator rule per variable) + optional **subclass** for logic (`kpi_ontology/adapters/acme_cm.py`).
- Every extracted number keeps its provenance (sheet!cell, raw text, method) in the run file.
- `commitments:` in the mapping capture the CM's own stated targets. `propose` turns them into contract constraints and records how many weeks each one held.
- `extra_fields:` capture report numbers the model does not use yet. `propose` turns them into new variables and KPIs under a new objective.
- `propose` also flags targets that have never been reached, tightens thresholds to demonstrated capability, and recalibrates simulation spreads from real week-to-week variation.
- Sample data: `python scripts/make_sample_cm_reports.py` writes 6 synthetic weekly reports to `data/inbox/`.

## Layout

| Path | What |
|---|---|
| `model/00_structure.yaml` | domains, process stages, objective tree |
| `model/10_variables.yaml` | raw measured inputs (illustrative baseline), with optional `dist` for prediction |
| `model/20_kpis.yaml` | KPI definitions: formula, unit, direction, target, threshold, weight, kind, mode |
| `model/30_gates_delivery.yaml` | process gates, program gates, deliverables and their dependencies |
| `model/15_questions.yaml` | who to ask, the question, and the definition to agree with the CM, per variable |
| `model/40_constraints.yaml` | contract, physical, data-integrity and policy constraints |
| `mappings/*.yaml` | one per report layout: report location -> variable |
| `data/inbox/` | incoming CM reports. `data/runs/` = one extracted run file per week |
| `proposals/` | drafts from `propose` (not loaded until moved to `extensions/`) |
| `extensions/*.yaml` | **your custom domains**, loaded after `model/` (see `example_esd_control.yaml`) |
| `data/` | real period values (`id,value` CSV or YAML) |
| `kpi_ontology/` | loader/validator, engine, exporters, CLI |

## Adding a custom domain

Copy `extensions/example_esd_control.yaml`. In one file you can:

1. Add a `domain` with any `type` and free-form `attributes`.
2. Add `variables` with your own `type` (for example `resistance`, `torque`, `enum`), `unit`, `source`, `dist`, and `attributes`.
3. Add `kpis`. A `formula` can reference variables **and** other KPI ids. The allowed functions are `min max abs round sqrt log exp clamp`, and `x if cond else y` also works.
4. Attach KPIs anywhere in the tree with `parent`.
5. Override any existing node by repeating its `id`. Fields are merged, so you can change only `target`.
6. Add criteria to existing gates with `gate_criteria_additions`.

Then run `python -m kpi_ontology validate`.

## Scoring rules

- KPI score = linear position between `threshold` (0) and `target` (1), clamped.
- Status: **green** = meets target, **amber** = between target and threshold, **red** = worse than threshold.
- Objective score = weighted mean of its children. Bands: ≥0.85 green, ≥0.5 amber, else red (`engine.HEALTH_*`).
- A gate passes when all `hard` criteria pass. `readiness` = the share of all criteria met.
