/**
 * Component grid — lists the 15 atomic components (C01..C15) grouped
 * by the row they occupy in the pipeline diagram.
 *
 * Server Component: fetches the catalog from the FastAPI backend at
 * request time (no caching — see ``cache: "no-store"`` in
 * ``server-fetch``). Each card links to
 * ``/components/[id]`` where the interactive run surface lives.
 */

import Link from "next/link";
import type { ComponentSummary } from "@/lib/types";
import { fetchComponents } from "@/lib/server-fetch";
import { kindChipClass, kindLabel } from "@/lib/format";

interface RowBucket {
  readonly row: 1 | 2 | 3 | 4;
  readonly title: string;
  readonly subtitle: string;
  readonly components: readonly ComponentSummary[];
}

function bucketByRow(
  components: readonly ComponentSummary[],
): readonly RowBucket[] {
  const groups: Record<1 | 2 | 3 | 4, ComponentSummary[]> = {
    1: [],
    2: [],
    3: [],
    4: [],
  };
  for (const component of components) {
    if (
      component.row === 1 ||
      component.row === 2 ||
      component.row === 3 ||
      component.row === 4
    ) {
      groups[component.row].push(component);
    }
  }
  for (const row of [1, 2, 3, 4] as const) {
    groups[row].sort((a, b) => a.id.localeCompare(b.id));
  }
  return [
    {
      row: 1,
      title: "Row 1 · Scenario & timing",
      subtitle: "Scenario draft → timing loop → refinement.",
      components: groups[1],
    },
    {
      row: 2,
      title: "Row 2 · Visuals & coherence",
      subtitle: "Content analysis → visual concepts → coherence gate → loop.",
      components: groups[2],
    },
    {
      row: 3,
      title: "Row 3 · Production & escalation",
      subtitle: "GPU production → assembly → escalation routing.",
      components: groups[3],
    },
    {
      row: 4,
      title: "Row 4 · Infrastructure",
      subtitle:
        "Per-VM services the pipeline rides on: guardian, worker registry, infra agent, worker VMs.",
      components: groups[4],
    },
  ];
}

export default async function ComponentsGridPage() {
  let components: readonly ComponentSummary[] = [];
  let error: string | null = null;
  try {
    components = await fetchComponents();
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  const rows = bucketByRow(components);

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col gap-10 px-8 py-12">
      <header className="flex flex-col gap-3 border-b border-pg-border pb-8">
        <p className="text-xs uppercase tracking-widest text-pg-muted">
          documentary-strands-migration · playground
        </p>
        <h1 className="text-3xl font-semibold text-pg-text">
          Components ({components.length})
        </h1>
        <p className="max-w-3xl text-pg-muted">
          Every atomic component declared in the registry. Click a card
          to load its cases, check model reachability, run a case, and
          evaluate the output.
        </p>
      </header>

      {error !== null && (
        <section
          role="alert"
          className="rounded border border-pg-red/40 bg-pg-red/10 px-4 py-3 text-sm text-pg-red"
        >
          <p className="font-mono text-xs">
            Backend unreachable — the playground UI needs the FastAPI
            server at <code>PLAYGROUND_API_URL</code> (defaults to{" "}
            <code>http://127.0.0.1:8000</code>). Start it with{" "}
            <code>poetry run uvicorn server.server:app --reload</code>.
          </p>
          <p className="mt-2 font-mono text-xs text-pg-red/80">{error}</p>
        </section>
      )}

      {rows.map((bucket) => (
        <RowSection key={bucket.row} bucket={bucket} />
      ))}
    </main>
  );
}

function RowSection({ bucket }: { readonly bucket: RowBucket }) {
  if (bucket.components.length === 0) {
    return null;
  }
  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold text-pg-text">{bucket.title}</h2>
        <p className="text-sm text-pg-muted">{bucket.subtitle}</p>
      </div>
      <ul className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {bucket.components.map((component) => (
          <ComponentCard key={component.id} component={component} />
        ))}
      </ul>
    </section>
  );
}

function ComponentCard({
  component,
}: {
  readonly component: ComponentSummary;
}) {
  return (
    <li className="flex h-full flex-col rounded border border-pg-border bg-pg-surface transition hover:border-pg-accent/60">
      <Link
        href={`/components/${component.id}`}
        className="flex h-full flex-col gap-3 p-4"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-xs uppercase tracking-wider text-pg-muted">
            {component.id}
          </span>
          <span
            className={`rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest ${kindChipClass(
              component.kind,
            )}`}
          >
            {kindLabel(component.kind)}
          </span>
        </div>
        <h3 className="text-base font-semibold text-pg-text">
          {component.title}
        </h3>
        <p className="flex-1 text-sm text-pg-muted">{component.summary}</p>
        <dl className="grid grid-cols-3 gap-2 border-t border-pg-border pt-3 text-xs">
          <Stat label="cases" value={component.case_count} />
          <Stat label="models" value={component.declared_models.length} />
          <Stat label="evals" value={component.evaluators.length} />
        </dl>
      </Link>
    </li>
  );
}

function Stat({
  label,
  value,
}: {
  readonly label: string;
  readonly value: number;
}) {
  return (
    <div className="flex flex-col">
      <dt className="text-[10px] uppercase tracking-widest text-pg-muted">
        {label}
      </dt>
      <dd className="font-mono text-sm text-pg-text">{value}</dd>
    </div>
  );
}
