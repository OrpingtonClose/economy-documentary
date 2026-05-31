"---
{
  "title": "Unit Agent and Integration Tests",
  "section": "21",
  "tags": [
    "architecture",
    "v7.1",
    "testing"
  ]
}
---

<- [[20 - Glossary|Glossary]] | [[00 - Index|Index]]

# 21. Unit Agent and Integration Tests

In V7.1, agents are autonomous HTTP services that communicate asynchronously through the SQLite Event Store and query state via the Global State Agent (GSA). To validate these components without breaking their design invariants, all tests are constructed as **HTTP endpoint-only simulation tests**. 

There is **no Python-level mocking (`unittest.mock.patch`)** of agent internal structures or event store databases. Mocking is restricted strictly to the external HTTP network boundaries (i.e. simulating VM workers, Vast.ai responses, or downstream agent triggers). 

---

## 21.1 Functionality of Individual Agents

Before defining unit agent tests, we document the precise functionality and expected behavior of each individual agent:

### 21.1.1 Scenario Agent (Port 8001)
* **First Draft Creation**: When the event store contains no narration script, the Scenario Agent writes the first draft script containing one or more narration blocks (specifying speaker, text, scripted duration, and scene layout).
* **Narrative Revision on Back-Edge**: If a downstream agent appends a `reconciliation_failed` event (with failure type `gap_unexpected` or `voice_mismatch` or `duration_unrecoverable`), the Scenario Agent reads the failure details and rewrites the affected narration blocks, appending a revised script.
* **Effect Extraction**: Emits `UpdateScript`, `DeleteScene`, `ReorderScenes`, `NoOp`, or `ClarificationRequest`.

### 21.1.2 Audio Agent (Port 8002)
* **TTS Job Generation**: Identifies blocks with `status="scripted"` and queues a TTS job (`QueueJob`) specifying target voice, speed, and text.
* **Duration Assessment & Tolerances**: Receives duration measurements from worker outputs and evaluates them against:
  $$\	ext{tolerance} = \max(\
<truncated 7586 bytes>