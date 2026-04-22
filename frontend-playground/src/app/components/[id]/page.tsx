/**
 * Component detail page — server shell + interactive client surface.
 *
 * The server fetches the component detail at request time (no
 * caching) and hands it to ``<ComponentWorkbench />`` as a plain
 * prop. The client component is responsible for the interactive
 * Health / Cases / Input / Run / Evaluate panes; this shell stays
 * thin so the fallback path (backend down, unknown id, etc.) can be
 * rendered purely server-side.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import type { ComponentDetail } from "@/lib/types";
import { fetchComponent } from "@/lib/server-fetch";
import { ComponentWorkbench } from "./ComponentWorkbench";

interface PageProps {
  readonly params: { readonly id: string };
}

export default async function ComponentDetailPage({ params }: PageProps) {
  let detail: ComponentDetail | null = null;
  let error: string | null = null;

  try {
    detail = await fetchComponent(params.id);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // A 404 from the backend means the component id isn't in the
    // registry. Any other error keeps the page rendered so the
    // reviewer sees the actual transport failure.
    if (/-> 404\b/.test(message)) {
      notFound();
    }
    error = message;
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col gap-6 px-8 py-10">
      <nav className="text-xs text-pg-muted">
        <Link href="/components" className="hover:text-pg-accent">
          ← all components
        </Link>
      </nav>

      {error !== null && (
        <section
          role="alert"
          className="rounded border border-pg-red/40 bg-pg-red/10 px-4 py-3 text-sm text-pg-red"
        >
          <p className="font-mono text-xs">
            Backend unreachable — the component detail endpoint at{" "}
            <code>GET /playground/components/{params.id}</code> did not
            respond. Start the FastAPI server with{" "}
            <code>poetry run uvicorn server.server:app --reload</code>.
          </p>
          <p className="mt-2 font-mono text-xs text-pg-red/80">{error}</p>
        </section>
      )}

      {detail !== null && <ComponentWorkbench detail={detail} />}
    </main>
  );
}
