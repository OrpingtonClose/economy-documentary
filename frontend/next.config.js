/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // UX-09 (#251): hide the Next.js dev indicators (build-activity dot
  // and the "route" / prerender toolbar) so a production-like local
  // build does not show an engineering widget floating above the
  // dashboard. The red error overlay itself is a portal element
  // (``<nextjs-portal>``) and is suppressed via CSS in globals.css;
  // see the companion skill note in .agents/skills/pipeline-architecture
  // for the full rationale and the known limitation that the overlay
  // cannot be fully disabled in ``next dev`` — the CSS override hides
  // it, but the portal still mounts and logs to the console.
  devIndicators: {
    buildActivity: false,
    appIsrStatus: false,
  },
};

module.exports = nextConfig;
