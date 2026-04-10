import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        pipeline: {
          bg: "#1a1a2e",
          card: "#16213e",
          accent: "#e94560",
          blue: "#0f3460",
          text: "#e0e0e0",
          muted: "#888888",
        },
      },
    },
  },
  plugins: [],
};
export default config;
