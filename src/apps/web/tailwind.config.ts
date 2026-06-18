import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        // Fraunces is the editorial display serif — plan headings, key prompts,
        // and serif accents. Loaded via Google Fonts @import in globals.css.
        display: ['"Fraunces"', "Georgia", "serif"],
        // Inter is the body/label sans. Loaded via Google Fonts @import in
        // globals.css; listed first so it wins over system-ui when available.
        sans: ['"Inter"', "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.35s ease-out both",
        shimmer: "shimmer 2.2s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
