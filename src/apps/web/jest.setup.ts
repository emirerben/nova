/**
 * Global jest setup — runs in every test file after the test environment is initialised.
 *
 * jsdom < 22 does not implement `crypto.randomUUID`; Node >= 15 does, but
 * jsdom overrides globalThis.crypto with its own implementation that may omit it.
 * Polyfill it here so any test that calls code using crypto.randomUUID works.
 */

/**
 * user-event's default delay is zero, but it still yields through a real
 * setTimeout between pointer actions and walks computed styles to validate
 * pointer-events. In jsdom, that adds scheduling and CSS-traversal overhead
 * to every pointer interaction, which can push Radix interactions past
 * Jest's five-second timeout on the hosted CI runner. Keep the event
 * sequence intact while removing that test-harness overhead.
 */
const userEventModule = require("@testing-library/user-event") as {
  default?: { setup: (options?: Record<string, unknown>) => unknown };
  setup?: (options?: Record<string, unknown>) => unknown;
};
const userEvent = (userEventModule.default ?? userEventModule) as {
  setup: (options?: Record<string, unknown>) => unknown;
};
const defaultUserEventSetup = userEvent.setup;
userEvent.setup = (options = {}) =>
  defaultUserEventSetup({
    ...options,
    delay: options.delay ?? null,
    ...(process.env.CI && {
      pointerEventsCheck: options.pointerEventsCheck ?? 0,
    }),
  });

// Feature flags: page.tsx reads these at module-load time via `=== "true"`.
// Without a .env in src/apps/web/, process.env is empty in jest → flags off →
// "timeline" tab hidden → tests that click the Timeline tab fail.
process.env.NEXT_PUBLIC_SOUND_EFFECTS_ENABLED = "true";
process.env.NEXT_PUBLIC_UNIFIED_TIMELINE_ENABLED = "true";
process.env.NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED = "true";
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

/**
 * Radix UI polyfills (Kria shadcn/ui foundation, DESIGN.md §15).
 *
 * jsdom implements none of ResizeObserver, pointer capture, or
 * `Element.scrollIntoView` — Radix's Select/DropdownMenu/Tooltip/ScrollArea
 * primitives call all three during open/close and positioning. Without these
 * stubs every test that opens one of those primitives throws
 * "X is not a function" before it can assert anything.
 */
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = ResizeObserverStub;
}

if (typeof Element.prototype.hasPointerCapture !== "function") {
  Element.prototype.hasPointerCapture = () => false;
}
if (typeof Element.prototype.setPointerCapture !== "function") {
  Element.prototype.setPointerCapture = () => {};
}
if (typeof Element.prototype.releasePointerCapture !== "function") {
  Element.prototype.releasePointerCapture = () => {};
}
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = () => {};
}

if (typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
