> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# CI_PIPELINE — GitHub Actions integration

Every PR that touches `server/strands_agents/` runs the full eval harness
for the affected component(s). This document specifies the workflow.

Pattern cribbed from
[`strands-devtools/cdk-evals/lambda/eval-runner/handler.py`](https://github.com/OrpingtonClose/strands-devtools/blob/main/cdk-evals/lambda/eval-runner/handler.py)
— we run the same code paths locally in CI that the Lambda eval runner
runs in prod.

---

## 1. Workflow layout

`.github/workflows/strands-evals.yml`

```yaml
name: strands-evals

on:
  pull_request:
    paths:
      - "server/strands_agents/**"
      - "server/strands_agents/evals/**"
      - "docs/strands-migration/eval-framework/THRESHOLDS.md"
      - ".github/workflows/strands-evals.yml"

concurrency:
  group: strands-evals-${{ github.ref }}
  cancel-in-progress: true

jobs:
  discover:
    runs-on: ubuntu-latest
    outputs:
      experiments: ${{ steps.find.outputs.experiments }}
    steps:
      - uses: actions/checkout@v4
      - id: find
        run: |
          # Emit a JSON matrix of experiment files to run, based on the PR diff
          python scripts/find_affected_experiments.py >> "$GITHUB_OUTPUT"

  run:
    needs: discover
    if: needs.discover.outputs.experiments != '[]'
    strategy:
      fail-fast: false
      matrix:
        experiment: ${{ fromJson(needs.discover.outputs.experiments) }}
    runs-on: ubuntu-latest
    env:
      STRANDS_MODEL: ${{ secrets.STRANDS_MODEL }}
      STRANDS_SYNTHESIS_MODEL: ${{ secrets.STRANDS_SYNTHESIS_MODEL }}
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      OPENAI_API_BASE: ${{ secrets.OPENAI_API_BASE }}
      PHOENIX_ENDPOINT: ${{ secrets.PHOENIX_ENDPOINT }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e . -e ./vendored/strands-evals
      - name: Run experiment
        run: |
          python -m server.strands_agents.evals.run_experiment \
            --experiment "${{ matrix.experiment }}" \
            --thresholds docs/strands-migration/eval-framework/THRESHOLDS.md \
            --report-out reports/$(basename "${{ matrix.experiment }}" .json)-report.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: eval-report-${{ matrix.experiment }}
          path: reports/
```

The matrix over experiments means one component failing doesn't block the
others' reports from being available for debugging.

---

## 2. `run_experiment.py` contract

A single entrypoint at `server/strands_agents/evals/run_experiment.py`:

```python
async def main(experiment_path: Path, thresholds_path: Path, report_out: Path) -> int:
    experiment = Experiment.from_file(str(experiment_path))
    task = _import_task_for(experiment_path)          # resolves to the Strands agent wrapper
    reports = await experiment.run_evaluations_async(task)
    thresholds = _parse_thresholds_markdown(thresholds_path)
    violations = _enforce(reports, thresholds, stage=_infer_stage(experiment_path))
    report_out.write_text(json.dumps([r.model_dump() for r in reports], indent=2))
    if violations:
        for v in violations:
            print(f"THRESHOLD VIOLATION: {v}", file=sys.stderr)
        return 1
    return 0
```

`_enforce` must:

1. Parse the markdown table in `THRESHOLDS.md` (no separate YAML; one
   source of truth).
2. For every `(stage, evaluator)` row, find the matching `EvaluationReport`
   and compare `overall_score` (for aggregate evaluators) or
   `min(scores)` (for per-case hard gates).
3. Return a list of human-readable violation strings.

Hard-gate rows (`Hard Gate? = Yes` in the table) must fail the job even if
the aggregate is above the min. They're the non-negotiable invariants.

---

## 3. Secrets

Required repo secrets (configure at `Settings → Secrets → Actions`):

| Secret | Notes |
|--------|-------|
| `STRANDS_MODEL` | Primary model string for evals (keep permissive, e.g. `openai/gpt-4o`) |
| `STRANDS_SYNTHESIS_MODEL` | Evaluator judge model (smaller is fine) |
| `OPENAI_API_KEY` | Venice-proxied key |
| `OPENAI_API_BASE` | e.g. `https://api.venice.ai/api/v1` |
| `PHOENIX_ENDPOINT` | OTel collector for evals tracing |

No AWS creds required for the default runner; GPU / TTS workers are
simulated via `ToolSimulator` (see [`SIMULATION.md`](./SIMULATION.md)).

---

## 4. Nightly run (real integrations)

Separate workflow `.github/workflows/strands-evals-nightly.yml` runs the
same experiments against **real** TTS + GPU workers:

- Trigger: `schedule: cron: "0 6 * * *"` (06:00 UTC = after off-hours US).
- Env: production-like (real `TTS_WORKER_URL`, `VIDEO_WORKER_URLS`).
- Output: report artifacts + Slack webhook on regression against last
  night's scores.
- Budget: one full pipeline run per night, capped at $20 compute spend
  via vast.ai quotas.

The nightly job is the canary for model drift, worker health regressions,
and model router misconfigurations the sim can't see.

---

## 5. Local reproducibility

Every experiment must run locally with:

```bash
python -m server.strands_agents.evals.run_experiment \
  --experiment server/strands_agents/evals/experiments/scenario_experiment.json \
  --thresholds docs/strands-migration/eval-framework/THRESHOLDS.md \
  --report-out /tmp/scenario-report.json
```

No args other than the experiment path. This is the same command CI runs,
so if it passes locally it passes in CI.
