/**
 * Pipeline orchestration page.
 *
 * Server Component shell — the real surface is the client component
 * ``PipelineOrchestrator`` so the form, SSE subscription, and live
 * stage ribbon can react to events without a full page round-trip.
 *
 * Until slice 9 attaches the real orchestrator, the backend route at
 * ``POST /playground/pipeline/runs`` runs the deterministic
 * ``SimulatedPipelineRun`` from slice 7 — the wire shape, event
 * vocabulary, and stage names match the real orchestrator so
 * everything downstream (UI, evaluators, traces) is exercised on the
 * same envelopes.
 */

import { PipelineOrchestrator } from "./PipelineOrchestrator";

export default function PipelinePage() {
  return <PipelineOrchestrator />;
}
