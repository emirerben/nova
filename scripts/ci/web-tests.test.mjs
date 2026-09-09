import assert from 'node:assert/strict';
import { test } from 'node:test';
import { assertSuccess, interactionPaths, partition } from './web-tests.mjs';

test('partition covers discovery exactly once, including new tests', () => {
  const discovered = [...interactionPaths, 'src/lib/__tests__/new.test.ts', 'src/__tests__/other.test.tsx'];
  const split = partition(discovered);
  assert.equal(Object.keys(split).length, 5);
  assert.deepEqual(Object.values(split).flat().sort(), discovered.sort());
  assert.equal(new Set(Object.values(split).flat()).size, discovered.length);
});
test('missing or duplicate discovered tests fail closed', () => {
  assert.throws(() => partition(interactionPaths.slice(1)), /Missing/);
  assert.throws(() => partition([...interactionPaths, interactionPaths[0]]), /Duplicate/);
});
test('gate accepts only an explicitly successful matrix', () => {
  assert.doesNotThrow(() => assertSuccess({ 'test-web-suites': { result: 'success' } }));
  for (const result of ['failure', 'cancelled', 'skipped', undefined]) {
    assert.throws(() => assertSuccess({ 'test-web-suites': { result } }));
  }
  assert.throws(() => assertSuccess({}));
});

test('similar names and colocated tests stay in the remaining suite', () => {
  const others = [
    'src/__tests__/components/Header.test.ts',
    'src/__tests__/plan/ChatCreationWorkspace-extra.test.tsx',
    'src/lib/timeline/__tests__/new.test.ts',
  ];
  assert.deepEqual(partition([...interactionPaths, ...others]).remaining, others);
});

test('gate CLI returns nonzero for each unsuccessful Actions result', async () => {
  const { spawnSync } = await import('node:child_process');
  const script = new URL('./web-tests.mjs', import.meta.url);
  const { fileURLToPath } = await import('node:url');
  for (const result of ['success', 'failure', 'cancelled', 'skipped']) {
    const child = spawnSync(process.execPath, [fileURLToPath(script), 'gate'], {
      env: { ...process.env, WEB_TEST_NEEDS: JSON.stringify({ 'test-web-suites': { result } }) },
      encoding: 'utf8',
    });
    assert.equal(child.status, result === 'success' ? 0 : 1, result);
  }
});

test('discovery uses the pnpm executable environment', async () => {
  const { mkdtempSync, writeFileSync, readFileSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { delimiter, join, resolve } = await import('node:path');
  const { fileURLToPath } = await import('node:url');
  const { spawnSync } = await import('node:child_process');
  const temp = mkdtempSync(join(tmpdir(), 'web-ci-pnpm-'));
  try {
    const calls = join(temp, 'calls.json');
    const web = fileURLToPath(new URL('../../src/apps/web/', import.meta.url));
    writeFileSync(join(temp, 'pnpm'), `#!/usr/bin/env node\nrequire('node:fs').writeFileSync(${JSON.stringify(calls)}, JSON.stringify(process.argv.slice(2)));\nconsole.log(${JSON.stringify(JSON.stringify(interactionPaths.map(path => resolve(web, path))))});\n`, { mode: 0o755 });
    const child = spawnSync(process.execPath, [fileURLToPath(new URL('./web-tests.mjs', import.meta.url)), 'verify'], {
      env: { ...process.env, PATH: `${temp}${delimiter}${process.env.PATH}` }, encoding: 'utf8',
    });
    assert.equal(child.status, 0, child.stderr);
    assert.deepEqual(JSON.parse(readFileSync(calls, 'utf8')), ['exec', 'jest', '--listTests', '--json', '--runInBand']);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});
