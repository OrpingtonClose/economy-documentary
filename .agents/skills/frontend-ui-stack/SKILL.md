# Frontend UI stack

This repository uses one — and only one — component library stack in `frontend/`.

## Stack

- **Next.js 14** (App Router) + **React 18** + **TypeScript**
- **Tailwind CSS v3** with CSS variables for theming
- **shadcn/ui** (neutral base color, `new-york` style) — primitives live in `frontend/src/components/ui/`
- **Radix UI** (pulled in transitively by shadcn primitives only)
- **lucide-react** for all icons
- **sonner** for toasts (`<Toaster/>` is mounted in `frontend/src/app/layout.tsx`)
- **mermaid** (via `frontend/src/components/mermaid-diagram.tsx`) for live architecture diagrams
- **CopilotKit** for the producer chat surface
- **Zustand** for client state
- `class-variance-authority`, `clsx`, `tailwind-merge` (utility deps for shadcn; do not replace)

## Do NOT add competing UI libraries

Adding any of the following is forbidden — PRs must be closed:

- Mantine (`@mantine/*`)
- Material UI / MUI (`@mui/*`, `@material-ui/*`)
- Chakra UI (`@chakra-ui/*`)
- Ant Design (`antd`, `@ant-design/*`)
- Bootstrap / react-bootstrap
- Semantic UI / Fomantic UI
- Blueprint (`@blueprintjs/*`)
- Headless UI (`@headlessui/*`) — use Radix (via shadcn) instead
- Styled-components, Emotion, stitches, vanilla-extract, or any other CSS-in-JS runtime — use Tailwind utility classes
- Any other icon library (Heroicons, FontAwesome, Material Icons, react-icons, etc.) — use **lucide-react** only
- Any other toast library (react-hot-toast, react-toastify, notistack) — use **sonner** only
- Any other diagram library for architecture maps (react-flow, dagre-d3, vis-network) — use **mermaid** only

Existing one-off deps (e.g. `@copilotkit/*`, `zustand`) are allowed and are not considered "competing UI libraries" — they cover orthogonal concerns (chat UI, state).

## Adding a new shadcn primitive

```
cd frontend
npx shadcn@2.4.0 add <primitive> -y
```

Commit the generated file in `frontend/src/components/ui/` and the new `@radix-ui/*` entries in `package.json`. Do **not** hand-edit primitives; re-run `add` with `--overwrite` to pull upstream fixes.

> Note: `shadcn@latest` (v4.x) does not detect this project's Next.js 14 + Tailwind 3 setup. Pin to `shadcn@2.4.0` for `init` and `add`.

## Theming

- Base color: `neutral` (see `components.json`).
- Theme vars live in `frontend/src/app/globals.css` under `:root` / `.dark`.
- `tailwind.config.ts` references vars directly via `var(--token)` (not wrapped in `hsl(...)`) because the CLI writes `oklch()` values.
- Do not add a third-party "theme provider" that fights Tailwind. Dark mode is class-based (`class="dark"` on `<html>`).

## Sanity page

`/ui-kit` (`frontend/src/app/ui-kit/page.tsx`) renders every primitive once. Keep it in sync when new primitives are added so reviewers see the design language at a glance.

## Scope boundary

Do **not** migrate existing components (OTIO timeline, CopilotChat wrappers, pipeline dashboard, etc.) inside stack-scaffolding PRs. Migration happens in dedicated children issues.
