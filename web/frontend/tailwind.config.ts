import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // NERD UI palette: deep neutrals + neon accents.
        keboola: {
          DEFAULT: "#22c55e",
          50: "#f0fdf4",
          400: "#4ade80",
          500: "#22c55e",
          600: "#16a34a",
          700: "#15803d",
        },
        accent: "#22d3ee",
        neon: {
          green: "#39ff14",
          pink: "#ff10f0",
          amber: "#ffaa00",
        },
      },
      fontFamily: {
        mono: ["'JetBrains Mono'", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
} satisfies Config;
