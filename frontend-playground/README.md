> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Documentary Playground (frontend)

Standalone Next.js workbench for the 15 atomic components of the
economy-documentary pipeline (C01–C15).

This is **not** the production frontend. It's a separate app that
talks only to `server/playground.py` and exists to let us poke each
component in isolation: run a registered case, run a custom input,
score the output against the declared evaluator stack, and (later)
promote a custom input to a committed case via a PR.

## Layout

```
frontend-playground/
├── src/
│   ├── app/
│   │   ├── layout.tsx     # app-router shell (dark, Tailwind)
│   │   ├── page.tsx       # landing page — scaffold status
│   │   └── globals.css    # Tailwind directives + pg-dot utilities
│   └── lib/
│       ├── types.ts       # hand-written TS contracts for /playground/*
│       └── api.ts         # fetch helpers for the five endpoints
├── next.config.js         # rewrites /playground/* to PLAYGROUND_API_URL
├── tailwind.config.ts     # pg-* design tokens (bg, surface, dots)
├── jest.config.cjs        # ts-jest + jsdom
└── tsconfig.json
```

## Running

```bash
cd frontend-playground
npm install
npm run dev    # http://localhost:3100
```

By default the app proxies `/playground/*` to `http://127.0.0.1:8000`.
Override with `PLAYGROUND_API_URL` to point at a different backend.

## Scope per PR

- **PR 6 (this PR)** — scaffold only: Next.js app, TS contracts,
  fetch helpers, layout shell, static landing page, helper tests.
  No component grid yet.
- **PR 7** — component grid + case browser + input editor + live
  run panes wired against the fetch helpers.
- **PR 8** — "Save as case" → PR flow for custom inputs.

## Contract drift

Types in `src/lib/types.ts` are deliberately hand-written. If the
backend envelope in `server/playground.py` changes, TypeScript
compilation fails here — which is the whole point of the playground
being its own app.
