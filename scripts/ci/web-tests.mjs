import { spawnSync } from 'node:child_process';
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const groups = {
  'interaction-1': ['plan/ChatCreationWorkspace', 'ui/tooltip'],
  'interaction-2': ['plan/PersonalizationPage', 'library/LibraryTile', 'plan/edit-proposal-card'],
  'interaction-3': ['plan/unified-timeline', 'plan/items/ToolDrawer-visuals', 'ui/select', 'plan/TikTokConnectionPanel'],
  'interaction-4': ['components/header', 'plan/items/inspector-panel-text-horizontal', 'ui/dropdown-menu', 'ui/popover'],
};
export const interactionPaths = Object.values(groups).flat().map(name => `src/__tests__/${name}.test.tsx`);

export function partition(discovered) {
  if (new Set(discovered).size !== discovered.length) throw new Error('Duplicate discovered suite');
  if (new Set(interactionPaths).size !== interactionPaths.length) throw new Error('Duplicate interaction suite');
  for (const path of interactionPaths) {
    if (!discovered.includes(path)) throw new Error(`Missing interaction suite: ${path}`);
  }
  return {
    ...Object.fromEntries(Object.entries(groups).map(([name, paths]) => [name, paths.map(path => `src/__tests__/${path}.test.tsx`)])),
    remaining: discovered.filter(path => !interactionPaths.includes(path)),
  };
}

export function assertSuccess(needs) {
  const expected = ['test-web-suites'];
  if (Object.keys(needs).length !== expected.length || expected.some(key => needs[key]?.result !== 'success')) {
    throw new Error(`Web test jobs did not all succeed: ${JSON.stringify(needs)}`);
  }
}

function main() {
  const command = process.argv[2];
  if (command === 'gate') {
    assertSuccess(JSON.parse(process.env.WEB_TEST_NEEDS || '{}'));
    return;
  }
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
  const cwd = resolve(root, 'src/apps/web');
  // pnpm's executable wrapper supplies NODE_PATH for transitive Next.js modules.
  // Invoking jest/bin/jest.js directly breaks styled-jsx resolution on pnpm CI.
  const discovery = spawnSync('pnpm', ['exec', 'jest', '--listTests', '--json', '--runInBand'], { cwd, encoding: 'utf8' });
  if (discovery.status !== 0) throw new Error(discovery.error?.message || discovery.stderr || discovery.stdout || 'Jest discovery failed');
  const discovered = JSON.parse(discovery.stdout).map(path => relative(cwd, path).replaceAll('\\', '/'));
  const partitions = partition(discovered);
  console.log(JSON.stringify(Object.fromEntries(Object.entries(partitions).map(([key, paths]) => [key, paths.length]))));
  if (command === 'verify') return;
  if (!Object.hasOwn(partitions, command)) throw new Error(`Unknown group: ${command}`);

  const outputDir = resolve(root, 'test-results/web', command);
  mkdirSync(outputDir, { recursive: true });
  const started = Date.now();
  const results = [];
  let failed = false;
  // Every interaction suite gets its own process. The remaining suites keep
  // Jest's normal worker pool and 5s test timeout.
  const batches = command === 'remaining' ? [partitions.remaining] : partitions[command].map(path => [path]);
  for (const [index, paths] of batches.entries()) {
    const output = resolve(outputDir, `jest-${index}.json`);
    const args = ['--ci', '--json', `--outputFile=${output}`, '--runTestsByPath', ...paths];
    if (command !== 'remaining') args.push('--runInBand', '--testTimeout=300000');
    const batchStarted = Date.now();
    const child = spawnSync('pnpm', ['exec', 'jest', ...args], { cwd, stdio: 'inherit' });
    if (child.error) console.error(child.error);
    let report;
    try { report = JSON.parse(readFileSync(output, 'utf8')); } catch { /* Preserve process failure below. */ }
    const ok = child.status === 0 && report?.success === true;
    failed ||= !ok;
    results.push({ paths, status: child.status, signal: child.signal, success: ok, seconds: (Date.now() - batchStarted) / 1000,
      tests: report?.numTotalTests ?? null,
      suites: report?.testResults?.map(suite => ({ path: relative(cwd, suite.name), status: suite.status, seconds: (suite.endTime - suite.startTime) / 1000 })) ?? [] });
    // Save after each process so earlier timings survive a later interruption.
    writeFileSync(resolve(outputDir, 'timings.json'), JSON.stringify({ group: command, discovered: discovered.length, selected: partitions[command].length, seconds: (Date.now() - started) / 1000, results }, null, 2));
  }
  if (process.env.GITHUB_STEP_SUMMARY) {
    appendFileSync(process.env.GITHUB_STEP_SUMMARY, `## Web tests: ${command}\n\n${partitions[command].length}/${discovered.length} suites; ${((Date.now() - started) / 1000).toFixed(1)}s; ${failed ? 'FAILED' : 'passed'}.\n\n| Batch | Seconds | Result |\n|---|---:|---|\n${results.map((r, i) => `| ${command === 'remaining' ? 'Remaining suite' : r.paths[0]} | ${r.seconds.toFixed(1)} | ${r.success ? 'passed' : 'FAILED'} |`).join('\n')}\n`);
  }
  if (failed) process.exitCode = 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
