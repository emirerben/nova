// esbuild bundler for the Kria Music Ingest extension.
//
// Mirrors the Phase 0 spike's bundler (validated MV3-CSP-safe — youtubei.js
// web build uses a pure-JS AST interpreter, no native eval/Function), with
// these production-grade tweaks:
//
//   - sourcemap: "inline" so production stack traces map to src/ lines.
//   - icon copy so the toolbar action renders the Kria mark.
//   - manifest.json minified-passthrough (no transformation; would otherwise
//     end up unparseable if we ran it through esbuild as JSON).

import * as esbuild from "esbuild";
import { nodeModulesPolyfillPlugin } from "esbuild-plugins-node-modules-polyfill";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const watchMode = process.argv.includes("--watch");

const SRC = join(__dirname, "src");
const DIST = join(__dirname, "dist");
const ICONS_SRC = join(__dirname, "icons");
const ICONS_DIST = join(DIST, "icons");

mkdirSync(DIST, { recursive: true });
mkdirSync(ICONS_DIST, { recursive: true });

// Copy static files (manifest, html) to dist.
for (const f of ["manifest.json", "offscreen.html", "popup.html"]) {
  const src = join(SRC, f);
  if (existsSync(src)) copyFileSync(src, join(DIST, f));
}
// Copy icons (any .png).
if (existsSync(ICONS_SRC)) {
  for (const f of readdirSync(ICONS_SRC)) {
    if (f.endsWith(".png")) {
      copyFileSync(join(ICONS_SRC, f), join(ICONS_DIST, f));
    }
  }
}

const sharedOptions = {
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["chrome120"],
  sourcemap: "inline",
  plugins: [
    nodeModulesPolyfillPlugin({
      globals: { Buffer: true },
      modules: {
        stream: true,
        crypto: true,
        buffer: true,
        events: true,
        util: true,
        url: true,
        querystring: true,
        path: true,
        process: "empty",
        fs: "empty",
        http: "empty",
        https: "empty",
        net: "empty",
        tls: "empty",
        zlib: "empty",
      },
    }),
  ],
  define: {
    "process.env.NODE_ENV": '"production"',
    "process.platform": '"browser"',
    "process.browser": "true",
    global: "globalThis",
  },
  logLevel: "info",
};

const entries = [
  { in: join(SRC, "background.js"), out: join(DIST, "background.js") },
  { in: join(SRC, "offscreen.js"), out: join(DIST, "offscreen.js") },
  { in: join(SRC, "popup.js"), out: join(DIST, "popup.js") },
  { in: join(SRC, "content.js"), out: join(DIST, "content.js") },
];

if (watchMode) {
  for (const e of entries) {
    const ctx = await esbuild.context({
      ...sharedOptions,
      entryPoints: [e.in],
      outfile: e.out,
    });
    await ctx.watch();
    console.log(`watching ${e.in}`);
  }
  console.log("build.mjs: watch mode active (Ctrl-C to stop)");
} else {
  for (const e of entries) {
    await esbuild.build({
      ...sharedOptions,
      entryPoints: [e.in],
      outfile: e.out,
    });
  }
  console.log("build.mjs: dist/ ready");
}
