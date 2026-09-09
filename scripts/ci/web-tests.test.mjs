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
