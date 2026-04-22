import type { Config } from "tailwindcss";

// Standalone Tailwind config — intentionally independent of the
// production frontend/ so the playground can evolve its own design
// language without breaking the main dashboard.
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Same background token as the production dashboard so the
        // two apps feel like a set. The chip colours below mirror the
        // test-case atlas (docs/strands-migration/diagrams/) so
        // reachability dots read identically across both surfaces.
        "pg-bg": "#0b0d10",
        "pg-surface": "#13161b",
        "pg-border": "#1f242c",
        "pg-text": "#e5e7eb",
        "pg-muted": "#94a3b8",
        "pg-green": "#22c55e",
        "pg-amber": "#eab308",
        "pg-red": "#ef4444",
        "pg-accent": "#38bdf8",
      },
      fontFamily: {
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};

export default config;
