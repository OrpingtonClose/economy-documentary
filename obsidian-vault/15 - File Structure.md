---
{
  "title": "File Structure",
  "section": "15",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[14 - Configuration|Configuration]] | [[00 - Index|Index]] | [[16 - Traceability and Observability|Traceability and Observability]] ->

# File Structure


### 15.1 Directory Layout

#### 15.1.1 Complete tree: server/v7/ with all files

The repository root is `server/v7/`. Files and directories are grouped by responsibility: top-level modules for orchestration, `agents/` for domain-specific generation logic, `provisioner/` for cloud VM lifecycle, and `vm/` for the on-instance agent runtime.

```text
server/v7/
├── README.md                          # Project overview and quick-start
├── ARCHITECTURE_V7.md                 # This document
├── docker-compose.yml                 # Agent services (EventStoreDB for distributed deployments)
├── config.py                          # Pydantic Config model (§14.1)
├── effects.py                         # 32 effect types + EffectUnion + KIND_TO_MODEL
├── event_store.py                     # SQLite EventStore class [[05 - Event Store|[[05 - Event Store|§5]]]]
├── projections.py                     # Read-model builders: OTIO, Job, VM, State, Budget
├── parser.py                          # Category-conditioned effect parser (§9.6)
├── structured_extract.py              # instructor integration + _MultiEffect models
├── agent_base.py                      # FastAPI app + create_pipeline_agent factory
├── global_state_agent.py              # Global State Agent: GET / only, serves projections (§2.4)
├── situations.py                      # SITUATION_TEMPLATES text blocks for agent prompts
├── rules.py                           # RULES text blocks for agent system prompts
├── agents/
│   ├── __init__.py
│   ├── scenario.py                    # ScenarioAgent: role instructions + focus
│   ├── audio.py                       # AudioAgent: tolerance + reconciliation focus
│   ├── video.py                       # VideoAgent: quality judgment + LTX focus
│   └── assembly.py                    # AssemblyAgent: ffmpeg + validation
├── provisioner/
│   ├── __init__.py
│   ├── main.py                        # FastAPI app for provisioner agent [[10 - Provisioner Agent|[[10 - Provisioner Agent|§10]]]]
│   └── bash.py                        # Direct bash execution primitives (no wrappers)
└── vm/
    ├── __init__.py
    ├── agent.py                         # On-instance daemon: fetch, execute, report [[11 - VM Worker|[[11 - VM Worker|§11]]]]
    ├── onstart_tts.sh                   # TTS VM bootstrap: conda env + model download
    └── onstart_ltx.sh                   # Video VM bootstrap: conda env + model download
```

**Top-level modules.** `config.py` contains the `Config` Pydantic model and is imported by `agent_base.py`, `global_state_agent.py`, and `provisioner/main.py`. `effects.py` defines the 32 `Effect` dataclass hierarchies and the `EffectUnion` discriminated union used by `parser.py`. `event_store.py` and `projections.py` form the persistence layer: the former appends domain events to DB files, the latter rebuilds read models. `agent_base.py` is the executable entry point for each media agent; it instantiates `Config`, creates the FastAPI app, and serves the HTTP endpoints. `global_state_agent.py` is the executable entry point for the Global State Agent (§2.4); it polls DB files and serves projections via `GET /`.

**`agents/` package.** Each module defines role instructions, focus functions, and permitted effects for one agent type. `assembly.py` performs FFmpeg muxing, WhisperX transcript alignment, and dual-threshold duration validation.

**`provisioner/` package.** `main.py` exposes the FastAPI app with `tick()` and webhook handlers. `bash.py` contains only generic async subprocess helpers — no Vast.ai-specific wrapper methods.

**`vm/` package.** `agent.py` is the only Python process running on rented instances. It receives tasks via POST, executes the command allowlist (§14.1.4), streams stdout/stderr back, and sends results to the Provisioner. The `onstart_*.sh` scripts are rendered as Vast.ai "on-start" scripts; they install Miniconda, create the environment, and download model weights.

### 15.2 Python Dependencies

```text
pydantic>=2.0
esdbclient>=1.0
opentimelineio>=0.16.0
instructor>=1.0.0
openai>=1.0.0
httpx>=0.27.0
fastapi>=0.110.0
uvicorn>=0.29.0
python-multipart>=0.0.9
pydantic-deep>=0.3.0
pydantic-ai-todo>=0.2.1
pydantic-ai-shields>=0.3.2
pydantic-ai-summarization>=0.1.4
pydantic-ai-provenance>=0.1.0
uuid_extensions>=0.0.10
```

Install: `pip install -r requirements.txt`

### 15.3 API Key Management

| Key | Secrets File Key | Used By |
|---|---|---|
| DeepSeek API | `deepseek_api_key` | All agents (LLM calls), parser |
| Vast.ai | `vastai_api_key` | Provisioner (VM allocation via bash) |


Keys are read from `secrets.json` at startup. The file is loaded once and passed
as a `Secrets` dataclass to all constructors. Never commit `secrets.json` to
version control.

```python
class Secrets(BaseModel):
    deepseek_api_key: str
    vastai_api_key: str
```

---

