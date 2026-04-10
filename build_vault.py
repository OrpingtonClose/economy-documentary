#!/usr/bin/env python3
"""
Obsidian vault builder — realtime and batch.

Two modes:
  1. Inline:  VaultBuilder instantiated by the pipeline. .on_claim() called
              after each claim completes. Writes claim page instantly, rebuilds
              indexes so Obsidian reflects progress in realtime.
  2. Batch:   python build_vault.py  (reads claim_stream.jsonl, builds all)

Usage:
    python build_vault.py [--stream PATH] [--scenes PATH] [--output PATH]
"""
import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path


# ── Defaults ──────────────────────────────────────────────────
DEFAULT_STREAM = Path(os.environ.get(
    "CLAIM_STREAM_PATH",
    "/tmp/documentary-pipeline/enrichment/claim_stream.jsonl",
))
DEFAULT_SCENES = Path(os.environ.get(
    "SCENES_PATH",
    "/tmp/documentary-pipeline/data/scenes_parsed.json",
))
DEFAULT_OUTPUT = Path(os.environ.get(
    "VAULT_OUTPUT_PATH",
    "/tmp/documentary-pipeline/vault",
))
DEFAULT_QUARTZ = Path(os.environ.get(
    "QUARTZ_CONTENT_PATH",
    "/tmp/documentary-pipeline/quartz-content",
))


# ── Helpers ───────────────────────────────────────────────────

def safe_filename(text: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', '-', text)


def sanitize(text: str) -> str:
    """Remove raw XML/HTML tags and JSON fragments that break Quartz."""
    text = re.sub(r'<[^>]+>', '', text)             # strip HTML/XML tags
    text = re.sub(r'```json\s*\{.*', '', text, flags=re.DOTALL)  # strip leaked JSON blocks
    text = text.replace('|', '\\|')                  # escape pipe for tables
    return text.strip()


def conf_emoji(conf: float) -> str:
    if conf >= 0.95:
        return "🟢"
    elif conf >= 0.7:
        return "🟡"
    elif conf >= 0.4:
        return "🟠"
    return "🔴"


def status_emoji(status: str) -> str:
    return {"verified": "✅", "partial": "⚠️", "disputed": "❌", "unverified": "❓"}.get(status, "❓")


def scene_fn(num: int, title: str) -> str:
    clean = safe_filename(title)
    return f"Scene {num:02d} — {clean}"


# ── Topic assignment ──────────────────────────────────────────

# Topic taxonomy — loaded from environment or config file, with economy defaults.
# To customize for a different documentary topic, set TOPIC_TAXONOMY_PATH env var
# pointing to a JSON file with {"Topic Name": ["keyword1", "keyword2", ...]} format.
_TOPIC_TAXONOMY_PATH = os.environ.get("TOPIC_TAXONOMY_PATH", "")


def _load_topic_taxonomy() -> dict[str, list[str]]:
    """Load topic taxonomy from file or use built-in defaults."""
    if _TOPIC_TAXONOMY_PATH and os.path.exists(_TOPIC_TAXONOMY_PATH):
        try:
            import json as _json
            with open(_TOPIC_TAXONOMY_PATH) as f:
                return _json.load(f)
        except Exception as e:
            import sys
            print(f"WARNING: Failed to load topic taxonomy from {_TOPIC_TAXONOMY_PATH}: {e}", file=sys.stderr)

    # Built-in defaults (economy documentary)
    return {
        "Federal Reserve": ["federal reserve", "fed ", "fed's", "fed chair", "monetary policy", "quantitative easing", "qe", "balance sheet", "interest rate", "rate hike", "fomc"],
        "Energy & Oil": ["oil", "energy", "gas ", "lng", "petroleum", "crude", "brent", "pipeline", "opec", "strait of hormuz", "refinery"],
        "Inflation": ["inflation", "cpi", "consumer price", "stagflation", "warflation", "deflat"],
        "National Debt": ["national debt", "deficit", "debt", "trillion dollar", "interest payment", "government borrow", "fiscal"],
        "Trade Policy": ["tariff", "trade", "ieepa", "scotus", "import", "export", "manufacturing"],
        "Precious Metals": ["gold", "silver", "precious metal", "monetary metal", "bullion", "central bank gold"],
        "Cryptocurrency": ["bitcoin", "crypto", "btc"],
        "Housing Market": ["housing", "mortgage", "home sale", "homeowner", "real estate", "rent"],
        "Stock Market": ["stock market", "s&p", "equity", "index fund", "etf"],
        "Bond Market": ["bond", "yield", "treasury", "fixed income"],
        "Consumer Economy": ["consumer", "grocery", "credit card", "spending", "sentiment"],
    }


TOPICS = _load_topic_taxonomy()


def assign_topics(text: str) -> list[str]:
    text_lower = text.lower()
    matched = []
    for topic, keywords in TOPICS.items():
        for kw in keywords:
            if kw in text_lower:
                matched.append(topic)
                break
    return matched


# ── Page builders ─────────────────────────────────────────────

def build_claim_page(rec: dict) -> str:
    """Full claim page with conditions, tool trace, reasoning."""
    cid = rec["claim_id"]
    status = rec["status"]
    conf = rec["confidence"]
    snum = rec["scene_num"]
    stitle = rec.get("scene_title", "")
    sfn = scene_fn(snum, stitle)
    topics = assign_topics(rec["claim_text"])

    lines = [
        f"# {cid}",
        "",
        f"**Status:** {status_emoji(status)} {status} | **Confidence:** {conf_emoji(conf)} {conf:.2f} | **Tools:** {rec['tool_calls_made']} | **Turns:** {rec['turns_used']}",
        f"**Scene:** [[Scenes/{sfn}|Scene {snum}: {stitle}]]",
        "",
        "## Claim",
        "",
        f"> {rec['claim_text']}",
        "",
    ]

    # Conditions — the substance
    conditions = rec.get("conditions", [])
    if conditions:
        lines.append(f"## Findings ({len(conditions)} conditions)")
        lines.append("")
        for i, c in enumerate(conditions, 1):
            fact = sanitize(c['fact'])
            if not fact:
                continue
            src = f" — [{c.get('source_url', '')}]({c.get('source_url', '')})" if c.get("source_url") else ""
            vstatus = f" `{c['verification_status']}`" if c.get("verification_status") else ""
            lines.append(f"{i}. {conf_emoji(c['confidence'])} **{c['confidence']:.1f}** — {fact}{src}{vstatus}")
        lines.append("")

    # Sources
    sources = rec.get("sources", [])
    if sources:
        lines.append(f"## Sources ({len(sources)})")
        lines.append("")
        for s in sources:
            lines.append(f"- [{s}]({s})")
        lines.append("")

    # Tool trace
    trace = rec.get("tool_trace", [])
    if trace:
        lines.append(f"## Tool Trace ({len(trace)} calls)")
        lines.append("")
        lines.append("| Turn | Tool | Args | Result | Time |")
        lines.append("|------|------|------|--------|------|")
        for t in trace:
            args_str = json.dumps(t.get("arguments", {}), default=str)
            if len(args_str) > 60:
                args_str = args_str[:57] + "..."
            if t.get("was_duplicate"):
                lines.append(f"| {t['turn']} | {t['tool_name']} | {args_str} | DUPLICATE | — |")
            else:
                lines.append(f"| {t['turn']} | {t['tool_name']} | {args_str} | {t.get('result_length', 0)} chars | {t.get('duration_sec', 0):.1f}s |")
        lines.append("")

    # Reasoning trace
    reasoning = rec.get("reasoning_trace", [])
    if reasoning:
        lines.append(f"## Reasoning ({len(reasoning)} steps)")
        lines.append("")
        for r in reasoning:
            action = r.get("action", "")
            turn = r.get("turn", "?")
            content = r.get("content", "")[:500].replace("\n", " ")
            novelty = r.get("novelty", -1)

            header = f"### Turn {turn} — {action}"
            if novelty >= 0:
                header += f" (novelty: {novelty:.2f})"
            if r.get("conditions_extracted"):
                header += f" | extracted: {r['conditions_extracted']}, admitted: {r['conditions_admitted']}"
            if r.get("tool_calls_requested"):
                header += f" | {r['tool_calls_requested']} tool calls"

            lines.append(header)
            lines.append("")
            content = sanitize(content)
            if content:
                lines.append(f"> {content}")
                lines.append("")

    # Topics
    if topics:
        lines.append("## Topics")
        lines.append("")
        for t in topics:
            lines.append(f"- [[Topics/{t}|{t}]]")
        lines.append("")

    lines.append("---")
    lines.append(f"← [[Scenes/{sfn}|Back to Scene {snum}]] | [[Dashboard]]")
    return "\n".join(lines)


# ── VaultBuilder — realtime incremental builder ───────────────

class VaultBuilder:
    """Maintains vault state. Call on_claim() after each claim completes.

    Writes the claim page immediately and rebuilds all index files so
    Obsidian and Quartz see progress in realtime.

    Outputs to two locations:
      - Obsidian vault (with .obsidian config)
      - Quartz content dir (for static site generation)
    """

    def __init__(self, output: Path, scenes_data: dict[int, dict],
                 quartz_content: Path = None):
        self.output = output
        self.quartz = quartz_content or DEFAULT_QUARTZ
        self.scenes_data = scenes_data  # {scene_num: scene_dict}
        self.records: list[dict] = []
        self.by_scene: dict[int, list[dict]] = defaultdict(list)
        self.by_topic: dict[str, list[dict]] = defaultdict(list)
        self.all_scene_titles: dict[int, str] = {}

        # Seed scene titles from scene data
        for snum, sd in scenes_data.items():
            self.all_scene_titles[snum] = sd.get("title", f"Scene {snum}")

        # Create directory structure for both outputs
        for root in [output, self.quartz]:
            for d in ["", "Scenes", "Claims", "Topics"]:
                os.makedirs(root / d, exist_ok=True)

        # Obsidian-specific config
        os.makedirs(output / ".obsidian", exist_ok=True)
        with open(output / ".obsidian" / "graph.json", "w") as f:
            json.dump({
                "colorGroups": [
                    {"query": "path:Scenes", "color": {"a": 1, "rgb": 3066993}},
                    {"query": "path:Claims", "color": {"a": 1, "rgb": 15105570}},
                    {"query": "path:Topics", "color": {"a": 1, "rgb": 8388736}},
                ],
                "showArrow": True, "linkDistance": 250, "repelStrength": 10,
            }, f, indent=2)

        # Quartz index page
        self._write(self.quartz / "index.md", "---\ntitle: Documentary Enrichment\n---\n\n![[Dashboard]]\n")

    def on_claim(self, claim: dict, rec: dict):
        """Called when a claim finishes. Writes claim page, rebuilds indexes.

        Args:
            claim: the original claim dict (id, text, scene_num, scene_title, ...)
            rec:   the stream record dict (same shape as a claim_stream.jsonl line)
        """
        # Store
        self.records.append(rec)
        snum = rec["scene_num"]
        self.by_scene[snum].append(rec)
        if snum not in self.all_scene_titles:
            self.all_scene_titles[snum] = rec.get("scene_title", f"Scene {snum}")
        for topic in assign_topics(rec["claim_text"]):
            self.by_topic[topic].append(rec)

        # Write claim page immediately (to both Obsidian and Quartz)
        cid = rec["claim_id"]
        page = build_claim_page(rec)
        self._write(self.output / "Claims" / f"{cid}.md", page)

        # Rebuild indexes (fast — just string assembly)
        self._rebuild_indexes()

    def _rebuild_indexes(self):
        """Rewrite all index files from current state."""
        scene_nums = sorted(self.all_scene_titles.keys())
        total = len(self.records)
        verified = sum(1 for r in self.records if r["status"] == "verified")
        disputed = sum(1 for r in self.records if r["status"] == "disputed")
        partial = sum(1 for r in self.records if r["status"] == "partial")
        unverified = sum(1 for r in self.records if r["status"] == "unverified")
        total_tools = sum(r.get("tool_calls_made", 0) for r in self.records)
        total_conditions = sum(len(r.get("conditions", [])) for r in self.records)

        # ── Dashboard ──
        dashboard = f"""# Documentary Enrichment Dashboard

> Realtime fact-checking — updates as claims complete.

## Stats

| Metric | Value |
|--------|-------|
| Claims processed | {total} |
| Verified ✅ | {verified} ({100*verified//max(total,1)}%) |
| Disputed ❌ | {disputed} ({100*disputed//max(total,1)}%) |
| Partial ⚠️ | {partial} |
| Unverified ❓ | {unverified} |
| Tool calls | {total_tools:,} |
| Conditions | {total_conditions:,} |
| Scenes with data | {len(self.by_scene)} / {len(scene_nums)} |

## Navigation

- [[Scene Index]]
- [[Verified Claims]] ({verified})
- [[Disputed Claims]] ({disputed})
- [[Topic Index]]

## Topics

| Topic | Claims | Link |
|-------|--------|------|
"""
        for topic in sorted(self.by_topic.keys()):
            dashboard += f"| {topic} | {len(self.by_topic[topic])} | [[Topics/{topic}|→]] |\n"

        self._write(self.output / "Dashboard.md", dashboard)

        # ── Scene Index ──
        si = "# Scene Index\n\n| # | Scene | Phase | Claims | ✅ | ❌ | Tools | Conds |\n|---|-------|-------|--------|---|---|-------|-------|\n"
        for snum in scene_nums:
            title = self.all_scene_titles[snum]
            sfn = scene_fn(snum, title)
            sc = self.by_scene.get(snum, [])
            sd = self.scenes_data.get(snum, {})
            phase = sd.get("phase", "—")
            v = sum(1 for c in sc if c["status"] == "verified")
            d = sum(1 for c in sc if c["status"] == "disputed")
            tc = sum(c.get("tool_calls_made", 0) for c in sc)
            nc = sum(len(c.get("conditions", [])) for c in sc)
            si += f"| {snum} | [[Scenes/{sfn}|{title}]] | {phase} | {len(sc)} | {v} | {d} | {tc} | {nc} |\n"
        si += "\n← [[Dashboard]]\n"
        self._write(self.output / "Scene Index.md", si)

        # ── Scene pages ──
        for snum in scene_nums:
            title = self.all_scene_titles[snum]
            sfn = scene_fn(snum, title)
            sd = self.scenes_data.get(snum, {})
            phase = sd.get("phase", "—")
            duration = sd.get("duration_sec", 0)
            sc = self.by_scene.get(snum, [])

            # Prev/next
            idx = scene_nums.index(snum)
            prev_link = f"← [[Scenes/{scene_fn(scene_nums[idx-1], self.all_scene_titles[scene_nums[idx-1]])}|Prev]]" if idx > 0 else ""
            next_link = f"[[Scenes/{scene_fn(scene_nums[idx+1], self.all_scene_titles[scene_nums[idx+1]])}|Next]] →" if idx < len(scene_nums) - 1 else ""
            nav = f"{prev_link} | [[Scene Index]] | {next_link}"

            v = sum(1 for c in sc if c["status"] == "verified")
            d = sum(1 for c in sc if c["status"] == "disputed")
            p = sum(1 for c in sc if c["status"] == "partial")

            lines = [
                f"# Scene {snum}: {title}", "", nav, "",
                f"**Phase:** {phase} | **Duration:** {duration}s | **Claims:** {len(sc)} ({v} ✅ {d} ❌ {p} ⚠️)",
                "",
            ]
            if sc:
                lines += ["## Claims", "",
                           "| ID | Status | Conf | Conds | Tools | Text |",
                           "|----|--------|------|-------|-------|------|"]
                for c in sorted(sc, key=lambda x: x["claim_id"]):
                    cid = c["claim_id"]
                    n_cond = len(c.get("conditions", []))
                    text = c["claim_text"][:100].replace("|", "\\|")
                    lines.append(f"| [[Claims/{cid}|{cid}]] | {status_emoji(c['status'])} | {conf_emoji(c['confidence'])} {c['confidence']:.2f} | {n_cond} | {c.get('tool_calls_made', 0)} | {text} |")
            else:
                lines.append("_No claims processed yet._")

            lines += ["", "---", nav]
            self._write(self.output / "Scenes" / f"{sfn}.md", "\n".join(lines))

        # ── Topic pages ──
        for topic, recs in sorted(self.by_topic.items()):
            v = sum(1 for r in recs if r["status"] == "verified")
            d = sum(1 for r in recs if r["status"] == "disputed")
            lines = [f"# {topic}", "", f"**Claims:** {len(recs)} ({v} ✅, {d} ❌)", "",
                     "| ID | Scene | Status | Conf | Conds | Text |",
                     "|----|-------|--------|------|-------|------|"]
            for r in sorted(recs, key=lambda x: (x["scene_num"], x["claim_id"])):
                sfn = scene_fn(r["scene_num"], r.get("scene_title", ""))
                n_cond = len(r.get("conditions", []))
                text = r["claim_text"][:80].replace("|", "\\|")
                lines.append(f"| [[Claims/{r['claim_id']}|{r['claim_id']}]] | [[Scenes/{sfn}|{r['scene_num']}]] | {status_emoji(r['status'])} | {conf_emoji(r['confidence'])} {r['confidence']:.2f} | {n_cond} | {text} |")
            lines += ["", "← [[Topic Index]] | [[Dashboard]]"]
            self._write(self.output / "Topics" / f"{topic}.md", "\n".join(lines))

        # ── Topic Index ──
        ti = "# Topic Index\n\n| Topic | Claims | Verified | Disputed |\n|-------|--------|----------|----------|\n"
        for topic in sorted(self.by_topic.keys()):
            recs = self.by_topic[topic]
            v = sum(1 for r in recs if r["status"] == "verified")
            d = sum(1 for r in recs if r["status"] == "disputed")
            ti += f"| [[Topics/{topic}|{topic}]] | {len(recs)} | {v} ✅ | {d} ❌ |\n"
        ti += "\n← [[Dashboard]]\n"
        self._write(self.output / "Topic Index.md", ti)

        # ── Verified / Disputed lists ──
        for label, filter_status in [("Verified", "verified"), ("Disputed", "disputed")]:
            filtered = [r for r in self.records if r["status"] == filter_status]
            filtered.sort(key=lambda x: (x["scene_num"], x["claim_id"]))
            lines = [f"# {label} Claims", "", f"{len(filtered)} claims.", ""]
            current_scene = None
            for r in filtered:
                if r["scene_num"] != current_scene:
                    current_scene = r["scene_num"]
                    stitle = r.get("scene_title", "")
                    sfn = scene_fn(current_scene, stitle)
                    lines.append(f"\n## [[Scenes/{sfn}|Scene {current_scene}: {stitle}]]\n")
                n_cond = len(r.get("conditions", []))
                lines.append(f"- [[Claims/{r['claim_id']}|{r['claim_id']}]] {conf_emoji(r['confidence'])} ({r['confidence']:.2f}, {n_cond} conditions) — {r['claim_text'][:120]}")
            lines.append("\n← [[Dashboard]]")
            self._write(self.output / f"{label} Claims.md", "\n".join(lines))

    def _write(self, path: Path, content: str):
        """Write to Obsidian vault and mirror to Quartz content."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        # Mirror to quartz — compute relative path from obsidian root
        try:
            rel = path.relative_to(self.output)
            quartz_path = self.quartz / rel
            quartz_path.parent.mkdir(parents=True, exist_ok=True)
            with open(quartz_path, "w", encoding="utf-8") as f:
                f.write(content)
        except (ValueError, OSError):
            pass  # path not under output, skip quartz mirror


# ── Stream loader (for batch mode) ───────────────────────────

def load_stream(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  WARN: skipping line {lineno}: {e}")
    return records


def load_scenes(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    with open(path, "r") as f:
        scenes = json.load(f)
    return {s["scene_num"]: s for s in scenes}


# ── Batch build ───────────────────────────────────────────────

def build_vault(stream_path: Path, scenes_path: Path, output_path: Path):
    print(f"Loading stream: {stream_path}")
    records = load_stream(stream_path)
    print(f"  {len(records)} claim records")

    scenes_data = load_scenes(scenes_path)
    print(f"  {len(scenes_data)} scenes")

    builder = VaultBuilder(output_path, scenes_data)

    for rec in records:
        # Reconstruct minimal claim dict from record
        claim = {
            "id": rec["claim_id"],
            "text": rec["claim_text"],
            "scene_num": rec["scene_num"],
            "scene_title": rec.get("scene_title", ""),
        }
        builder.on_claim(claim, rec)

    file_count = sum(len(files) for _, _, files in os.walk(output_path))
    print(f"\nVault: {output_path}")
    print(f"  {file_count} files, {len(records)} claim pages")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Obsidian vault from claim stream")
    parser.add_argument("--stream", type=Path, default=DEFAULT_STREAM)
    parser.add_argument("--scenes", type=Path, default=DEFAULT_SCENES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_vault(args.stream, args.scenes, args.output)
