#!/usr/bin/env node
// Mirror the backend font registry + .ttf files into the web app.
//
// Backend canonical:
//   src/apps/api/assets/fonts/font-registry.json
//   src/apps/api/assets/fonts/*.ttf
//
// Web copies (consumed by the editor + admin layout @font-face):
//   src/apps/web/src/data/font-registry.json     <- imported by overlay-constants.ts
//   src/apps/web/public/fonts/*.ttf              <- served as /fonts/<file>.ttf
//
// Modes:
//   --check  : exit 1 if anything is out of sync (CI)
//   default  : copy and exit 0

import { readFileSync, writeFileSync, readdirSync, existsSync, mkdirSync, copyFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = resolve(__dirname, "..");
const API_FONTS = resolve(WEB_ROOT, "../api/assets/fonts");
const WEB_DATA = resolve(WEB_ROOT, "src/data");
const WEB_PUBLIC_FONTS = resolve(WEB_ROOT, "public/fonts");

const CHECK_MODE = process.argv.includes("--check");

function readBytes(p) {
  return readFileSync(p);
}
function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  return a.equals(b);
}

const problems = [];
function report(msg) {
  problems.push(msg);
  console.error(msg);
}

// 1. JSON registry must match exactly.
const apiRegistryPath = join(API_FONTS, "font-registry.json");
const webRegistryPath = join(WEB_DATA, "font-registry.json");
if (!existsSync(apiRegistryPath)) {
  report(`backend registry missing: ${apiRegistryPath}`);
} else {
  const apiBytes = readBytes(apiRegistryPath);
  if (!existsSync(webRegistryPath)) {
    if (CHECK_MODE) {
      report(`web registry missing: ${webRegistryPath}`);
    } else {
      mkdirSync(WEB_DATA, { recursive: true });
      writeFileSync(webRegistryPath, apiBytes);
      console.log(`copied font-registry.json -> ${webRegistryPath}`);
    }
  } else {
    const webBytes = readBytes(webRegistryPath);
    if (!sameBytes(apiBytes, webBytes)) {
      if (CHECK_MODE) {
        report(
          "font-registry.json drift between backend and web. Run `npm run sync:fonts` and commit.",
        );
      } else {
        writeFileSync(webRegistryPath, apiBytes);
        console.log(`updated ${webRegistryPath}`);
      }
    }
  }
}

// 2. Every .ttf referenced in the registry must exist in BOTH backend and web/public.
let registry = null;
try {
  registry = JSON.parse(readFileSync(apiRegistryPath, "utf8"));
} catch (err) {
  report(`could not parse backend registry: ${err.message}`);
}
if (registry && registry.fonts) {
  if (!existsSync(WEB_PUBLIC_FONTS)) {
    mkdirSync(WEB_PUBLIC_FONTS, { recursive: true });
  }
  for (const [name, entry] of Object.entries(registry.fonts)) {
    const file = entry.file;
    if (!file) {
      report(`registry entry "${name}" missing "file"`);
      continue;
    }
    const apiTtf = join(API_FONTS, file);
    const webTtf = join(WEB_PUBLIC_FONTS, file);
    if (!existsSync(apiTtf)) {
      report(`backend .ttf missing for "${name}": ${apiTtf}`);
      continue;
    }
    const apiBytes = readBytes(apiTtf);
    if (!existsSync(webTtf)) {
      if (CHECK_MODE) {
        report(`web .ttf missing for "${name}": ${webTtf}`);
      } else {
        writeFileSync(webTtf, apiBytes);
        console.log(`copied ${file}`);
      }
    } else {
      const webBytes = readBytes(webTtf);
      if (!sameBytes(apiBytes, webBytes)) {
        if (CHECK_MODE) {
          report(`web .ttf differs from backend for "${name}": ${file}`);
        } else {
          writeFileSync(webTtf, apiBytes);
          console.log(`updated ${file}`);
        }
      }
    }
  }
}

if (CHECK_MODE && problems.length) {
  console.error(`\nfont registry sync check failed (${problems.length} problem${problems.length === 1 ? "" : "s"}).`);
  process.exit(1);
}
console.log(CHECK_MODE ? "font registry in sync" : "font registry sync done");
