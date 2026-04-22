"use client";

/**
 * Interactive workbench for one atomic component.
 *
 * Three panes, left-to-right on wide screens, stacked on narrow:
 *
 *   1. Header + declared models with live reachability dots.
 *   2. Case list + input editor (pre-filled from the selected case).
 *   3. Run / evaluate output + save-as-case dialog.
 *
 * All state is local to the page — the playground is intentionally
 * session-scoped. Save-as-case commits through
 * ``POST /components/{id}/user-cases`` which writes to the on-disk
 * sidecar under ``server/strands_agents/playground/user_cases/``.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  CaseSummary,
  ComponentDetail,
  EvaluateResponse,
  HealthResponse,
  ReachabilityEntry,
  RunResponse,
  SaveUserCasePreview,
} from "@/lib/types";
import {
  evaluateCase,
  getComponentHealth,
  runCase,
  saveUserCase,
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

type CaseRole = "pass" | "neg" | "edge";

export function ComponentWorkbench({ detail }: Props) {
  // User cases live in component state so a commit updates the list
  // without a full page reload. The initial value comes from the
  // SSR'd detail payload, which the backend populates from the
  // sidecar JSON.
  const [userCases, setUserCases] = useState<readonly CaseSummary[]>(
    detail.user_cases ?? [],
  );

  // Selection is tracked by index into the combined (canonical +
  // user) array because some cases carry ``name: null`` and
  // name-based comparison lights up every unnamed case at once.
  const allCases = useMemo<readonly CaseSummary[]>(
    () => [...detail.cases, ...userCases],
    [detail.cases, userCases],
  );
  const canonicalCount = detail.cases.length;

  const initialIndex = useMemo(
    () => firstSelectableCaseIndex(allCases),
    [allCases],
  );
  const initialCase =
    initialIndex !== null ? (allCases[initialIndex] ?? null) : null;

  const [selectedCaseIndex, setSelectedCaseIndex] = useState<number | null>(
    initialIndex,
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

  const [saveDialogOpen, setSaveDialogOpen] = useState(false);

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
    (summary: CaseSummary, index: number) => {
      setSelectedCaseIndex(index);
      setInputText(prettyJson(summary.input));
      setRunResult(null);
      setRunError(null);
      setEvalResult(null);
      setEvalError(null);
    },
    [setSelectedCaseIndex],
  );

  const onRun = useCallback(async () => {
    // Clear previous run state up front so the Evaluate button
    // (gated on ``runResult.status === "OK"``) cannot dispatch a
    // stale ``actual_output`` while a fresh run is in flight or
    // after a validation failure.
    setRunResult(null);
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
      // on-the-wire payload reflects what they see. ``case_name``
      // is only sent when the selected case is named — the backend
      // has no way to address an unnamed case by key. User cases
      // are resolved server-side through the same ``case_name``
      // lookup path as canonical cases.
      const selected = caseAt(allCases, selectedCaseIndex);
      const selectedInputMatchesTextarea =
        selected !== null &&
        prettyJson(selected.input).trim() === inputText.trim();
      const body =
        selectedInputMatchesTextarea &&
        selected !== null &&
        selected.name !== null
          ? { case_name: selected.name }
          : { custom_input: parsed.value };
      const response = await runCase(detail.id, body);
      setRunResult(response);
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsRunning(false);
    }
  }, [allCases, detail.id, inputText, selectedCaseIndex]);

  const onEvaluate = useCallback(async () => {
    if (runResult === null) {
      return;
    }
    setEvalError(null);
    setIsEvaluating(true);
    try {
      const selected = caseAt(allCases, selectedCaseIndex);
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
  }, [allCases, detail.id, runResult, selectedCaseIndex]);

  const onSaveCommitted = useCallback(
    (stamped: CaseSummary) => {
      // Append to the local user-case list and move selection to
      // the new entry so Evaluate/Run can be retried against the
      // just-committed name.
      setUserCases((prev) => {
        const next = [...prev, stamped];
        // Newly appended entry lives at (canonical length + prev length).
        setSelectedCaseIndex(canonicalCount + prev.length);
        return next;
      });
      setSaveDialogOpen(false);
    },
    [canonicalCount],
  );

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
            userCases={userCases}
            canonicalCount={canonicalCount}
            selectedCaseIndex={selectedCaseIndex}
            onSelect={loadCase}
          />
        </div>
        <div className="flex flex-col gap-6">
          <InputEditor
            value={inputText}
            onChange={setInputText}
            onRun={onRun}
            onEvaluate={onEvaluate}
            onSaveAsCase={() => setSaveDialogOpen(true)}
            canEvaluate={runResult !== null && runResult.status === "OK"}
            isRunning={isRunning}
            isEvaluating={isEvaluating}
          />
          <RunResultPanel result={runResult} error={runError} />
          <EvaluateResultPanel result={evalResult} error={evalError} />
        </div>
      </div>
      {saveDialogOpen && (
        <SaveCaseDialog
          componentId={detail.id}
          currentInput={inputText}
          onClose={() => setSaveDialogOpen(false)}
          onCommitted={onSaveCommitted}
        />
      )}
    </div>
  );
}

function firstSelectableCaseIndex(
  cases: readonly CaseSummary[],
): number | null {
  for (let i = 0; i < cases.length; i += 1) {
    const candidate = cases[i];
    if (candidate.name !== null && candidate.name.length > 0) {
      return i;
    }
  }
  return cases.length > 0 ? 0 : null;
}

function caseAt(
  cases: readonly CaseSummary[],
  index: number | null,
): CaseSummary | null {
  if (index === null || index < 0 || index >= cases.length) {
    return null;
  }
  return cases[index] ?? null;
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
  userCases,
  canonicalCount,
  selectedCaseIndex,
  onSelect,
}: {
  readonly cases: ComponentDetail["cases"];
  readonly userCases: readonly CaseSummary[];
  readonly canonicalCount: number;
  readonly selectedCaseIndex: number | null;
  readonly onSelect: (summary: CaseSummary, index: number) => void;
}) {
  return (
    <section className="rounded border border-pg-border bg-pg-surface p-4">
      <h2 className="text-sm font-semibold text-pg-text">
        Cases ({cases.length + userCases.length})
      </h2>
      <p className="mt-1 text-xs text-pg-muted">
        Green = pass path, red = negative, amber = edge. User-saved
        cases appear below the canonical corpus and carry a
        <span className="ml-1 font-mono text-pg-accent">saved</span> tag.
      </p>
      <ul className="mt-3 flex max-h-96 flex-col gap-1 overflow-y-auto">
        {cases.map((summary, index) => (
          <CaseRow
            key={`canonical-${index}`}
            summary={summary}
            index={index}
            active={index === selectedCaseIndex}
            onSelect={onSelect}
          />
        ))}
        {userCases.length > 0 && (
          <li className="mt-3 flex items-center gap-2 text-[10px] uppercase tracking-widest text-pg-muted">
            <span className="h-px flex-1 bg-pg-border" />
            <span className="font-mono">saved by you ({userCases.length})</span>
            <span className="h-px flex-1 bg-pg-border" />
          </li>
        )}
        {userCases.map((summary, userIndex) => {
          const index = canonicalCount + userIndex;
          return (
            <CaseRow
              key={`user-${userIndex}`}
              summary={summary}
              index={index}
              active={index === selectedCaseIndex}
              onSelect={onSelect}
              isUser
            />
          );
        })}
      </ul>
    </section>
  );
}

function CaseRow({
  summary,
  index,
  active,
  onSelect,
  isUser,
}: {
  readonly summary: CaseSummary;
  readonly index: number;
  readonly active: boolean;
  readonly onSelect: (summary: CaseSummary, index: number) => void;
  readonly isUser?: boolean;
}) {
  const label = summary.name ?? `(unnamed case ${index + 1})`;
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(summary, index)}
        className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition ${
          active
            ? "bg-pg-accent/20 text-pg-accent"
            : "text-pg-text hover:bg-pg-border"
        }`}
      >
        <span className={`pg-dot ${caseRoleDot(summary.role)}`} />
        <span className="flex-1 truncate font-mono">{label}</span>
        {isUser && (
          <span className="rounded bg-pg-accent/10 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-widest text-pg-accent">
            saved
          </span>
        )}
      </button>
    </li>
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
  onSaveAsCase,
  canEvaluate,
  isRunning,
  isEvaluating,
}: {
  readonly value: string;
  readonly onChange: (next: string) => void;
  readonly onRun: () => void;
  readonly onEvaluate: () => void;
  readonly onSaveAsCase: () => void;
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
      <div className="mt-3 flex flex-wrap gap-2">
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
        <button
          type="button"
          onClick={onSaveAsCase}
          className="ml-auto rounded border border-dashed border-pg-accent/60 px-3 py-1.5 text-xs font-semibold text-pg-accent transition hover:bg-pg-accent/10"
        >
          Save as case…
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

/**
 * Two-step save flow: ``Preview`` posts with ``confirm=false`` and
 * shows the unified diff the commit would produce; ``Commit`` posts
 * the same body with ``confirm=true`` and writes to disk. Splitting
 * the interaction into preview → commit keeps the irreversible
 * side-effect one click further from a mistaken input — and the
 * backend enforces the same contract server-side, so a user who
 * bypasses the dialog (e.g. via cURL) still sees the preview first.
 */
function SaveCaseDialog({
  componentId,
  currentInput,
  onClose,
  onCommitted,
}: {
  readonly componentId: string;
  readonly currentInput: string;
  readonly onClose: () => void;
  readonly onCommitted: (committed: CaseSummary) => void;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<CaseRole>("pass");
  const [notes, setNotes] = useState("");
  const [preview, setPreview] = useState<SaveUserCasePreview | null>(null);
  const [pending, setPending] = useState<"preview" | "commit" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Any edit to the saved-case body invalidates an earlier preview —
  // otherwise the user could preview ``name=test_a`` and commit
  // ``name=test_b`` while staring at a diff for ``test_a``. Gate Commit
  // behind ``preview !== null`` and force a fresh preview on every
  // field change so the on-screen diff always matches what Commit
  // would send.
  useEffect(() => {
    setPreview(null);
  }, [name, role, notes, currentInput]);

  const buildBody = useCallback(
    (confirm: boolean) => {
      const parsed = parseJsonSafe(currentInput);
      if (!parsed.ok) {
        throw new Error(`Input is not valid JSON: ${parsed.error}`);
      }
      // An empty textarea yields ``value === undefined``. ``JSON.stringify``
      // silently drops ``undefined`` properties, so sending that straight
      // through would land at the backend with no ``input`` key and
      // surface as a generic 422 "field required". Fail early with a
      // clear message instead.
      if (parsed.value === undefined) {
        throw new Error(
          "Input is required — paste a JSON payload before saving.",
        );
      }
      return {
        name: name.trim(),
        role,
        input: parsed.value,
        notes: notes.trim() || undefined,
        confirm,
      };
    },
    [currentInput, name, notes, role],
  );

  const onPreview = useCallback(async () => {
    setError(null);
    setPending("preview");
    try {
      const body = buildBody(false);
      const response = await saveUserCase(componentId, body);
      setPreview(response.preview);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  }, [buildBody, componentId]);

  const onCommit = useCallback(async () => {
    setError(null);
    setPending("commit");
    try {
      const body = buildBody(true);
      const response = await saveUserCase(componentId, body);
      if (response.committed && response.case) {
        onCommitted(response.case);
      } else {
        setError("Server reported committed=false — check logs.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  }, [buildBody, componentId, onCommitted]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Save as case"
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-6"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[90vh] w-full max-w-3xl flex-col gap-4 overflow-y-auto rounded border border-pg-border bg-pg-bg p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-pg-text">
            Save as case · <span className="font-mono">{componentId}</span>
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-pg-border px-2 py-1 text-xs text-pg-muted transition hover:border-pg-accent/60"
          >
            Close
          </button>
        </div>
        <p className="text-xs text-pg-muted">
          Saved cases land in
          <span className="ml-1 font-mono text-pg-text">
            server/strands_agents/playground/user_cases/{componentId}.json
          </span>
          . Case names must be unique across both the canonical and
          user corpora for this component.
        </p>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <label className="flex flex-col gap-1 text-xs text-pg-muted">
            <span>Case name</span>
            <input
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. user_fog_scene_01"
              className="rounded border border-pg-border bg-pg-surface px-2 py-1.5 font-mono text-xs text-pg-text focus:border-pg-accent focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-pg-muted">
            <span>Role</span>
            <select
              value={role}
              onChange={(event) => setRole(event.target.value as CaseRole)}
              className="rounded border border-pg-border bg-pg-surface px-2 py-1.5 font-mono text-xs text-pg-text focus:border-pg-accent focus:outline-none"
            >
              <option value="pass">pass · green path</option>
              <option value="neg">neg · negative expected</option>
              <option value="edge">edge · boundary</option>
            </select>
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs text-pg-muted">
          <span>Notes (optional)</span>
          <textarea
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            rows={2}
            placeholder="Why this case matters. Visible in the case list."
            className="rounded border border-pg-border bg-pg-surface px-2 py-1.5 text-xs text-pg-text focus:border-pg-accent focus:outline-none"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onPreview}
            disabled={pending !== null || !name.trim()}
            className="rounded border border-pg-border px-3 py-1.5 text-xs font-semibold text-pg-text transition hover:border-pg-accent/60 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {pending === "preview" ? "Previewing…" : "Preview diff"}
          </button>
          <button
            type="button"
            onClick={onCommit}
            disabled={pending !== null || !name.trim() || preview === null}
            className="rounded bg-pg-accent px-3 py-1.5 text-xs font-semibold text-pg-bg transition hover:bg-pg-accent/80 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {pending === "commit" ? "Committing…" : "Commit"}
          </button>
        </div>
        {error !== null && (
          <p
            role="alert"
            className="rounded border border-pg-red/40 bg-pg-red/10 p-2 font-mono text-xs text-pg-red"
          >
            {error}
          </p>
        )}
        {preview !== null && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between text-xs text-pg-muted">
              <span className="font-mono">{preview.file_path}</span>
              <span>
                {preview.case_count_before} → {preview.case_count_after} cases
              </span>
            </div>
            <pre className="max-h-72 overflow-auto rounded border border-pg-border bg-pg-surface p-2 font-mono text-[11px] text-pg-text">
              {preview.diff || "(no diff — file will be created)"}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
