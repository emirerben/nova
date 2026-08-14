import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { copyFile, mkdir, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(here, "../node_modules/canvaskit-wasm/bin");
const source = resolve(packageRoot, "canvaskit.wasm");
const javascriptSource = resolve(packageRoot, "canvaskit.js");
const destination = resolve(here, "../public/_motion/canvaskit.wasm");
const expected = "2abfa191f92f0aee6e0c8e3ff9612294a7721a40761216867c1c059e7993c9d3";
const expectedJavascript =
  "b2556106b80c5ff3041f3888d55e602636e1812c98cf77a72e7c328c8036c838";
const checkOnly = process.argv.includes("--check");

const contractGenerator = resolve(
  here,
  "../../../packages/motion-runtime/scripts/generate-contract.mjs",
);
const contractCheck = spawnSync(process.execPath, [contractGenerator, "--check"], {
  encoding: "utf8",
});
if (contractCheck.status !== 0) {
  throw new Error(
    `Motion contract generation drift:\n${contractCheck.stderr || contractCheck.stdout}`,
  );
}

const bytes = await readFile(source);
const actual = createHash("sha256").update(bytes).digest("hex");
if (actual !== expected) {
  throw new Error(`CanvasKit WASM digest mismatch: expected ${expected}, got ${actual}`);
}
const javascriptBytes = await readFile(javascriptSource);
const actualJavascript = createHash("sha256").update(javascriptBytes).digest("hex");
if (actualJavascript !== expectedJavascript) {
  throw new Error(
    `CanvasKit JS digest mismatch: expected ${expectedJavascript}, got ${actualJavascript}`,
  );
}
if (!checkOnly) {
  await mkdir(dirname(destination), { recursive: true });
  await copyFile(source, destination);
}
