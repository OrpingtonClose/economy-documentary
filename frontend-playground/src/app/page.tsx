/**
 * Playground landing page — scaffold only.
 *
 * PR 6 intentionally ships infrastructure (types, fetch helpers,
 * layout shell, one landing page) without a functional component
 * browser. PR 7 wires the component grid + case browser + input
 * editor + live run panes against these helpers; PR 8 adds the
 * "Save as case" → PR flow.
 *
 * Rendering here is deliberately minimal and server-side: a short
 * description of what this app is, plus a checklist of what each
 * upcoming PR will bring. The purpose of the landing page is to
 * prove that the frontend-playground/ Next.js app builds and boots
 * against the scaffold, and to give the reviewer something to see
 * at ``http://localhost:3100`` once ``next dev -p 3100`` starts.
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
        <h2 className="text-lg font-semibold text-pg-text">
          Scaffold status (PR 6)
        </h2>
        <ul className="flex flex-col gap-2 text-sm text-pg-muted">
          <li>
            <span className="pg-dot pg-dot-green mr-2 align-middle" />
            Next.js 14 app router, TypeScript, Tailwind — booting on
            port 3100.
          </li>
          <li>
            <span className="pg-dot pg-dot-green mr-2 align-middle" />
            Hand-written TS contracts for every endpoint the backend
            (<code className="text-pg-accent">server/playground.py</code>) serves.
          </li>
          <li>
            <span className="pg-dot pg-dot-green mr-2 align-middle" />
            Fetch helpers for the five endpoints: catalog, component
            detail, health, run, evaluate.
          </li>
          <li>
            <span className="pg-dot pg-dot-amber mr-2 align-middle" />
            <strong className="text-pg-text">Next up (PR 7):</strong>
            &nbsp;component grid, case browser, input editor, live run
            panes wired against these helpers.
          </li>
          <li>
            <span className="pg-dot pg-dot-amber mr-2 align-middle" />
            <strong className="text-pg-text">After that (PR 8):</strong>
            &nbsp;“Save as case” → PR flow for custom inputs.
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
GET  /playground/components/{id}/health
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
