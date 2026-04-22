import Link from "next/link";

/**
 * Playground landing page.
 *
 * After PR 7 this is a thin entry point — the real surface is
 * ``/components`` (grid of all 15 components) and
 * ``/components/[id]`` (per-component workbench). The landing page
 * is kept so the app has a root route at ``/``, with a prominent
 * link into the grid and a compact summary of what the playground
 * offers.
 */
export default function PlaygroundLanding() {
  return (
    <main className="mx-auto flex min-h-screen max-w-5xl flex-col gap-8 px-8 py-12">
      <header className="flex flex-col gap-3 border-b border-pg-border pb-8">
        <p className="text-xs uppercase tracking-widest text-pg-muted">
          documentary-strands-migration · playground
        </p>
        <h1 className="text-3xl font-semibold text-pg-text">
          Component Playground
        </h1>
        <p className="max-w-3xl text-pg-muted">
          Standalone workbench for the 15 atomic components of the
          documentary pipeline (C01–C15). Every component exposes its
          declared models, its case library, its evaluator stack, and
          a live run surface — one page per component, no orchestrator
          in the loop.
        </p>
      </header>

      <section className="flex flex-col gap-4">
        <Link
          href="/components"
          className="inline-flex w-fit items-center gap-2 rounded bg-pg-accent px-4 py-2 text-sm font-semibold text-pg-bg transition hover:bg-pg-accent/80"
        >
          Browse the 15 components →
        </Link>
        <ul className="flex flex-col gap-2 text-sm text-pg-muted">
          <li>
            <span className="pg-dot pg-dot-green mr-2 align-middle" />
            Each component page shows its declared models, live
            reachability dots, evaluator stack, and case library.
          </li>
          <li>
            <span className="pg-dot pg-dot-green mr-2 align-middle" />
            Click a case to pre-fill the input editor, then hit{" "}
            <span className="font-mono text-pg-text">Run</span> to
            dispatch against the backend.
          </li>
          <li>
            <span className="pg-dot pg-dot-green mr-2 align-middle" />
            Edit the JSON freely — custom inputs are sent as{" "}
            <code>custom_input</code> and evaluated against the
            declared evaluators just like replay cases.
          </li>
        </ul>
      </section>

      <section className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold text-pg-text">
          Backend surface
        </h2>
        <pre className="overflow-x-auto rounded border border-pg-border bg-pg-surface p-4 text-xs text-pg-muted">
{`GET  /playground/components
GET  /playground/components/{id}
GET  /playground/components/{id}/models/health
POST /playground/components/{id}/run
POST /playground/components/{id}/evaluate`}
        </pre>
        <p className="text-xs text-pg-muted">
          next.config.js proxies <code>/playground/*</code> from this
          app’s dev server (port 3100) to the FastAPI backend set via{" "}
          <code>PLAYGROUND_API_URL</code> (defaults to{" "}
          <code>http://127.0.0.1:8000</code>).
        </p>
      </section>
    </main>
  );
}
