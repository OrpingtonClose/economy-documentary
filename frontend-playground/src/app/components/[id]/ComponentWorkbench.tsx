"use client";

/**
 * Interactive workbench for one atomic component.
 *
 * Three panes, left-to-right on wide screens, stacked on narrow:
 *
 *   1. Header + declared models with live reachability dots.
 *   2. Case list + input editor (pre-filled from the selected case).
 *   3. Run / evaluate output.
 *
 * All state is local to the page — the playground is intentionally
 * session-scoped. Saving a custom case as a PR is the job of PR 8.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  CaseSummary,
  ComponentDetail,
  EvaluateResponse,
  HealthResponse,
  ReachabilityEntry,
  RunResponse,
} from "@/lib/types";
import {
  evaluateCase,
  getComponentHealth,
  runCase,
} from "@/lib/api";
import {
  evaluateStatusClass,
  formatScore,
  kindChipClass,
  kindLabel,
  parseJsonSafe,
  prettyJson,
  runStatusClass,
} from "@/lib/format";

interface Props {
  readonly detail: ComponentDetail;
}

export function ComponentWorkbench({ detail }: Props) {
  const initialCase = useMemo(() => firstSelectableCase(detail), [detail]);

  const [selectedCaseName, setSelectedCaseName] = useState<string | null>(
    initialCase?.name ?? null,
  );
  const [inputText, setInputText] = useState<string>(() =>
    initialCase ? prettyJson(initialCase.input) : "",
  );
  const [runResult, setRunResult] = useState<RunResponse | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [evalResult, setEvalResult] = useState<EvaluateResponse | null>(null);
  const [evalError, setEvalError] = useState<string | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadHealth() {
      try {
        const response = await getComponentHealth(detail.id);
        if (!cancelled) {
          setHealth(response);
          setHealthError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setHealthError(err instanceof Error ? err.message : String(err));
        }
      }
    }
    void loadHealth();
    return () => {
      cancelled = true;
    };
  }, [detail.id]);

  const loadCase = useCallback(
    (summary: CaseSummary) => {
      setSelectedCaseName(summary.name);
      setInputText(prettyJson(summary.input));
      setRunResult(null);
      setRunError(null);
      setEvalResult(null);
      setEvalError(null);
    },
    [setSelectedCaseName],
  );

  const onRun = useCallback(async () => {
    setRunError(null);
    setEvalResult(null);
    setEvalError(null);
    const parsed = parseJsonSafe(inputText);
    if (!parsed.ok) {
      setRunError(`Input is not valid JSON: ${parsed.error}`);
      return;
    }
    setIsRunning(true);
    try {
      // The server accepts either ``case_name`` (replay) or
      // ``custom_input`` (arbitrary payload). When the user has
      // edited the textarea we send ``custom_input`` so the
      // on-the-wire payload reflects what they see.
      const selected = findCase(detail, selectedCaseName);
      const selectedInputMatchesTextarea =
        selected !== null &&
        prettyJson(selected.input).trim() === inputText.trim();
      const body = selectedInputMatchesTextarea
        ? { case_name: selected.name ?? undefined }
        : { custom_input: parsed.value };
      const response = await runCase(detail.id, body);
      setRunResult(response);
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRunning(false);
    }
  }, [detail, inputText, selectedCaseName]);

  const onEvaluate = useCallback(async () => {
    if (runResult === null) {
      return;
    }
    setEvalError(null);
    setIsEvaluating(true);
    try {
      const selected = findCase(detail, selectedCaseName);
      const response = await evaluateCase(detail.id, {
        case_name: selected?.name ?? undefined,
        actual_output: runResult.output,
        actual_trajectory: runResult.trajectory,
      });
      setEvalResult(response);
    } catch (err) {
      setEvalError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsEvaluating(false);
    }
  }, [detail, runResult, selectedCaseName]);

  return (
    <div className="flex flex-col gap-6">
      <Header detail={detail} />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[2fr_3fr]">
        <div className="flex flex-col gap-6">
          <HealthPanel
            models={detail.declared_models}
            health={health}
            error={healthError}
          />
          <CaseList
            cases={detail.cases}
            selectedCaseName={selectedCaseName}
            onSelect={loadCase}
          />
        </div>
        <div className="flex flex-col gap-6">
          <InputEditor
            value={inputText}
            onChange={setInputText}
            onRun={onRun}
            onEvaluate={onEvaluate}
            canEvaluate={runResult !== null && runResult.status === "OK"}
            isRunning={isRunning}
            isEvaluating={isEvaluating}
          />
          <RunResultPanel result={runResult} error={runError} />
          <EvaluateResultPanel result={evalResult} error={evalError} />
        </div>
      </div>
    </div>
  );
}

function firstSelectableCase(
  detail: ComponentDetail,
): CaseSummary | null {
  for (const candidate of detail.cases) {
    if (candidate.name !== null && candidate.name.length > 0) {
      return candidate;
    }
  }
  return detail.cases[0] ?? null;
}

function findCase(
  detail: ComponentDetail,
  name: string | null,
): CaseSummary | null {
  if (name === null) {
    return null;
  }
  return detail.cases.find((c) => c.name === name) ?? null;
}

function Header({ detail }: { readonly detail: ComponentDetail }) {
  return (
    <header className="flex flex-col gap-3 border-b border-pg-border pb-6">
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-mono text-xs uppercase tracking-widest text-pg-muted">
          {detail.id}
        </span>
        <span
          className={`rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest ${kindChipClass(
            detail.kind,
          )}`}
        >
          {kindLabel(detail.kind)}
        </span>
        <span className="font-mono text-[10px] uppercase tracking-widest text-pg-muted">
          row {detail.row}
        </span>
      </div>
      <h1 className="text-2xl font-semibold text-pg-text">{detail.title}</h1>
      <p className="max-w-3xl text-sm text-pg-muted">{detail.summary}</p>
    </header>
  );
}

function HealthPanel({
  models,
  health,
  error,
}: {
  readonly models: ComponentDetail["declared_models"];
  readonly health: HealthResponse | null;
  readonly error: string | null;
}) {
  const byId = new Map<string, ReachabilityEntry>();
  if (health !== null) {
    for (const entry of health.models) {
      byId.set(entry.model_id, entry);
    }
  }
  return (
    <section className="rounded border border-pg-border bg-pg-surface p-4">
      <h2 className="text-sm font-semibold text-pg-text">Declared models</h2>
      <p className="mt-1 text-xs text-pg-muted">
        Reachability probe runs on page load. Any unreachable model is
        a hard-gate failure at run time.
      </p>
      {models.length === 0 && (
        <p className="mt-3 text-xs italic text-pg-muted">
          No models declared — this component is deterministic.
        </p>
      )}
      <ul className="mt-3 flex flex-col gap-2">
        {models.map((model) => {
          const status = byId.get(model.id);
          const reachable = status?.reachable === true;
          const dotClass = reachable
            ? "pg-dot-green"
            : status === undefined
              ? "pg-dot-amber"
              : "pg-dot-red";
          return (
            <li
              key={`${model.provider}-${model.id}`}
              className="flex items-center justify-between gap-3 text-xs"
            >
              <div className="flex items-center gap-2">
                <span className={`pg-dot ${dotClass}`} />
                <span className="font-mono text-pg-text">{model.id}</span>
                <span className="text-pg-muted">· {model.provider}</span>
                <span className="text-pg-muted">· {model.role}</span>
              </div>
              {status !== undefined && status.latency_ms !== null && (
                <span className="font-mono text-[10px] text-pg-muted">
                  {Math.round(status.latency_ms)}ms
                </span>
              )}
              {status !== undefined && !reachable && status.reason !== null && (
                <span className="truncate font-mono text-[10px] text-pg-red">
                  {status.reason}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {error !== null && (
        <p className="mt-3 font-mono text-[10px] text-pg-red">{error}</p>
      )}
    </section>
  );
}

function CaseList({
  cases,
  selectedCaseName,
  onSelect,
}: {
  readonly cases: ComponentDetail["cases"];
  readonly selectedCaseName: string | null;
  readonly onSelect: (summary: CaseSummary) => void;
}) {
  return (
    <section className="rounded border border-pg-border bg-pg-surface p-4">
      <h2 className="text-sm font-semibold text-pg-text">
        Cases ({cases.length})
      </h2>
      <p className="mt-1 text-xs text-pg-muted">
        Click a case to load its input into the editor. Green = pass
        path, red = negative, amber = edge.
      </p>
      <ul className="mt-3 flex max-h-96 flex-col gap-1 overflow-y-auto">
        {cases.map((summary, index) => {
          const label = summary.name ?? `(unnamed case ${index + 1})`;
          const active = summary.name === selectedCaseName;
          return (
            <li key={`${label}-${index}`}>
              <button
                type="button"
                onClick={() => onSelect(summary)}
                className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition ${
                  active
                    ? "bg-pg-accent/20 text-pg-accent"
                    : "text-pg-text hover:bg-pg-border"
                }`}
              >
                <span className={`pg-dot ${caseRoleDot(summary.role)}`} />
                <span className="font-mono">{label}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function caseRoleDot(role: string): string {
  if (role === "pass") {
    return "pg-dot-green";
  }
  if (role === "neg") {
    return "pg-dot-red";
  }
  return "pg-dot-amber";
}

function InputEditor({
  value,
  onChange,
  onRun,
  onEvaluate,
  canEvaluate,
  isRunning,
  isEvaluating,
}: {
  readonly value: string;
  readonly onChange: (next: string) => void;
  readonly onRun: () => void;
  readonly onEvaluate: () => void;
  readonly canEvaluate: boolean;
  readonly isRunning: boolean;
  readonly isEvaluating: boolean;
}) {
  return (
    <section className="rounded border border-pg-border bg-pg-surface p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-pg-text">Input</h2>
        <p className="text-[10px] text-pg-muted">JSON · editable</p>
      </div>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        className="mt-3 h-64 w-full resize-y rounded border border-pg-border bg-pg-bg p-2 font-mono text-xs text-pg-text focus:border-pg-accent focus:outline-none"
        aria-label="Case input JSON"
      />
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={onRun}
          disabled={isRunning}
          className="rounded bg-pg-accent px-3 py-1.5 text-xs font-semibold text-pg-bg transition hover:bg-pg-accent/80 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isRunning ? "Running…" : "Run"}
        </button>
        <button
          type="button"
          onClick={onEvaluate}
          disabled={!canEvaluate || isEvaluating}
          className="rounded border border-pg-border px-3 py-1.5 text-xs font-semibold text-pg-text transition hover:border-pg-accent/60 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isEvaluating ? "Evaluating…" : "Evaluate"}
        </button>
      </div>
    </section>
  );
}

function RunResultPanel({
  result,
  error,
}: {
  readonly result: RunResponse | null;
  readonly error: string | null;
}) {
  if (error !== null) {
    return (
      <section
        role="alert"
        className="rounded border border-pg-red/40 bg-pg-red/10 p-4"
      >
        <h2 className="text-sm font-semibold text-pg-red">Run failed</h2>
        <p className="mt-2 font-mono text-xs text-pg-red">{error}</p>
      </section>
    );
  }
  if (result === null) {
    return (
      <section className="rounded border border-dashed border-pg-border bg-transparent p-4 text-xs text-pg-muted">
        Click <span className="font-mono">Run</span> to dispatch the
        current input against this component.
      </section>
    );
  }
  return (
    <section className="rounded border border-pg-border bg-pg-surface p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-pg-text">Run result</h2>
        <span
          className={`rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest ${runStatusClass(
            result.status,
          )}`}
        >
          {result.status}
        </span>
      </div>
      {result.error !== undefined && (
        <p className="mt-2 font-mono text-xs text-pg-red">{result.error}</p>
      )}
      {result.unreachable_models !== undefined &&
        result.unreachable_models.length > 0 && (
          <ul className="mt-2 flex flex-col gap-1 text-xs">
            {result.unreachable_models.map((m) => (
              <li key={m.model_id} className="font-mono text-pg-amber">
                {m.model_id} · {m.reason ?? "unreachable"}
              </li>
            ))}
          </ul>
        )}
      <details className="mt-3" open>
        <summary className="cursor-pointer text-xs text-pg-muted">
          Output
        </summary>
        <pre className="mt-2 max-h-72 overflow-auto rounded bg-pg-bg p-2 font-mono text-[11px] text-pg-text">
          {prettyJson(result.output)}
        </pre>
      </details>
      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-pg-muted">
          Trajectory
        </summary>
        <pre className="mt-2 max-h-72 overflow-auto rounded bg-pg-bg p-2 font-mono text-[11px] text-pg-text">
          {prettyJson(result.trajectory)}
        </pre>
      </details>
    </section>
  );
}

function EvaluateResultPanel({
  result,
  error,
}: {
  readonly result: EvaluateResponse | null;
  readonly error: string | null;
}) {
  if (error !== null) {
    return (
      <section
        role="alert"
        className="rounded border border-pg-red/40 bg-pg-red/10 p-4"
      >
        <h2 className="text-sm font-semibold text-pg-red">Evaluate failed</h2>
        <p className="mt-2 font-mono text-xs text-pg-red">{error}</p>
      </section>
    );
  }
  if (result === null) {
    return null;
  }
  return (
    <section className="rounded border border-pg-border bg-pg-surface p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-pg-text">Evaluator stack</h2>
        <span
          className={`rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest ${evaluateStatusClass(
            result.status,
          )}`}
        >
          {result.status} · {result.overall_passed ? "passed" : "failed"}
        </span>
      </div>
      {result.results.length === 0 && (
        <p className="mt-3 text-xs italic text-pg-muted">
          No evaluator rows returned.
        </p>
      )}
      <ul className="mt-3 flex flex-col gap-2">
        {result.results.map((row) => (
          <li
            key={row.name}
            className="flex items-center justify-between gap-2 rounded border border-pg-border bg-pg-bg px-3 py-2 text-xs"
          >
            <div className="flex items-center gap-2">
              <span
                className={`pg-dot ${
                  row.passed ? "pg-dot-green" : "pg-dot-red"
                }`}
              />
              <span className="font-mono text-pg-text">{row.name}</span>
              {row.hard_gate && (
                <span className="rounded bg-pg-amber/20 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-widest text-pg-amber">
                  hard
                </span>
              )}
            </div>
            <div className="flex items-center gap-3 text-pg-muted">
              <span className="font-mono">
                score {formatScore(row.mean_score)}
              </span>
              <span className="font-mono">
                ≥ {row.threshold.toFixed(2)}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
