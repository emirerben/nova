/**
 * Global jest setup — runs in every test file after the test environment is initialised.
 *
 * jsdom < 22 does not implement `crypto.randomUUID`; Node >= 15 does, but
 * jsdom overrides globalThis.crypto with its own implementation that may omit it.
 * Polyfill it here so any test that calls code using crypto.randomUUID works.
 */
if (typeof globalThis.crypto?.randomUUID !== "function") {
  let _uid = 0;
  const existing =
    (globalThis as Record<string, unknown>).crypto != null
      ? { ...(globalThis.crypto as object) }
      : {};
  Object.defineProperty(globalThis, "crypto", {
    value: { ...existing, randomUUID: () => `test-uuid-${++_uid}` },
    configurable: true,
    writable: true,
  });
}
