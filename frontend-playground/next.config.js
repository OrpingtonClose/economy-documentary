/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Mirror the production frontend/ app: no build-activity dot, no
  // ISR status widget. The playground is a workbench — engineering
  // overlays make the evaluator chips harder to read at a glance.
  devIndicators: {
    buildActivity: false,
    appIsrStatus: false,
  },
  async rewrites() {
    // Proxy the playground HTTP surface to the FastAPI backend so the
    // browser only ever talks to a single origin during development.
    // Override ``PLAYGROUND_API_URL`` when the backend lives
    // elsewhere (e.g. a deployed preview).
    const target = process.env.PLAYGROUND_API_URL || "http://127.0.0.1:8000";
    return [
      {
        source: "/playground/:path*",
        destination: `${target}/playground/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
