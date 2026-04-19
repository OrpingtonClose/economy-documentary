# ADK eval harness

Automated regression harness for the documentary pipeline, built on the
[Google Agent Development Kit][adk] eval tooling.

* **`adk web`** — interactive dashboard that fronts the orchestrator,
  shows every tool call / intermediate step, and can save a chat as a
  `.evalset.json` golden.
* **`adk eval`** — replays a golden against the agent and compares the
  actual tool trajectory + final response to the expected values, with
  configurable thresholds.

See the [ADK eval codelab][codelab] and the [ADK docs][adk-docs] for
background.

## Layout

```
server/adk_eval/
├── __init__.py                 # package marker, imports agent.py
├── agent.py                    # exports root_agent = pipeline_agent
├── test_config.json            # eval thresholds (start permissive, tighten later)
├── evalsets/
│   └── happy_path_brief_submission.evalset.json  # stubbed placeholder
├── test_evalsets.py            # pytest runner (skips stubs, runs real goldens)
└── README.md
```

`agent.py` re-exports `pipeline_agent` from `agents.pipeline` — the exact
same orchestrator `server.py` boots — so goldens regress production
behaviour, not a fork.

## Run `adk web` locally

```bash
cd server
poetry run adk web .
```

`adk web` expects an *agents directory* whose subdirectories are agent
modules; `server/adk_eval/` satisfies that shape. Open the URL it prints,
pick **adk_eval** in the agent dropdown, chat with the pipeline, then use
**Save as eval set** to drop a new `*.evalset.json` into
`server/adk_eval/evalsets/`.

> **Known issue (pre-existing, tracked separately):** under
> `google-adk==1.29.0` the `pipeline_agent` import currently fails because
> `agents/pipeline.py` parents every sub-agent to the ephemeral
> `_arch_b2_wiring_root` holder before constructing the real
> `SequentialAgent("documentary_pipeline")`. Fix that before relying on
> `adk web`; the pytest harness works around it by skipping stubbed cases
> when credentials or imports are unavailable.

## Capture a golden

1. `cd server && poetry run adk web .`
2. In the dashboard, pick the `adk_eval` agent.
3. Submit a brief and walk the pipeline through the stage you want to
   pin (e.g. brief → scenario draft).
4. Click **Save current session as eval set**. Choose a descriptive
   name, e.g. `happy_path_three_scene_brief`.
5. Move the saved file into `server/adk_eval/evalsets/` and commit it.
6. Strip or set `"metadata": {"stubbed": false, ...}` — otherwise the
   pytest runner will skip it.

## Run the harness

### Pytest (in-process)

```bash
cd server
poetry run pytest adk_eval/ -v
```

* Stubbed eval sets (e.g. `metadata.stubbed == true`) are skipped with a
  clear reason.
* Real eval sets require at least one of `GOOGLE_API_KEY`,
  `OPENAI_API_KEY`, or `OPENAI_API_BASE` to be set. Without credentials,
  the runner skips rather than hard-fails — the harness must stay
  green offline.

### CLI (ad hoc)

```bash
cd server
poetry run adk eval adk_eval adk_eval/evalsets/<file>.evalset.json \
  --config_file_path adk_eval/test_config.json
```

## Thresholds

`test_config.json` starts permissive:

```json
{
  "criteria": {
    "tool_trajectory_avg_score": 0.6,
    "response_match_score": 0.4
  }
}
```

`tool_trajectory_avg_score` is exact-match ratio on the tool-call
trajectory; `response_match_score` is ROUGE on the final assistant
reply. Tighten these as goldens stabilise.

## CI

The harness runs on every PR that touches `server/` via
`.github/workflows/adk-eval.yml`. On failure, actual-vs-expected diffs
(and any eval output artefacts under `/tmp/adk_eval_runs/`) are uploaded
as a workflow artifact so reviewers can inspect without rerunning.

[adk]: https://google.github.io/adk-docs/
[adk-docs]: https://google.github.io/adk-docs/
[codelab]: https://codelabs.developers.google.com/adk-eval/instructions
