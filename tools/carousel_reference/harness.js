// harness.js — deterministic clock + scripted-gesture + per-frame trace
// capture for the Blossom Carousel browser reference.
//
// MUST be loaded as a plain classic <script> BEFORE the Blossom module
// script tag. Classic (non-module, non-defer) scripts execute immediately
// in document order; <script type="module"> is always deferred until after
// HTML parsing, so a plain <script src="./harness.js"> placed earlier in
// the document is guaranteed to install the performance.now/rAF overrides
// before Blossom's drag engine (or anything else) ever reads the clock.
//
// Contract (see tools/carousel_reference/README.md for the full writeup):
//   window.__startGesture()  -> begin replay of gesture-trace.json
//   window.__step()          -> advance one deterministic 30fps frame;
//                                dispatches the next pointer event (if any)
//                                THEN flushes the rAF queue THEN records a
//                                trace entry. Returns the frame index.
//   window.__settled         -> true once scrollLeft has been stable for
//                                10 consecutive __step() calls after the
//                                pointerup.
//   window.__getTrace()      -> JSON.stringify(window.__trace)
//   window.__markReady()     -> called by each page once Blossom has
//                                initialized; appends <div id="ready">.
(() => {
  const FRAME_MS = 1000 / 30;
  const START_CLIENT_X = 540;
  const START_CLIENT_Y = 960;
  const SETTLE_STREAK_TARGET = 10;
  const SETTLE_EPSILON = 1e-6;

  let __t = 0;
  let __rafId = 1;
  let __queue = []; // [{id, cb}]

  // --- Deterministic clock -------------------------------------------------
  performance.now = () => __t;
  Date.now = () => __t;

  window.requestAnimationFrame = (cb) => {
    const id = __rafId++;
    __queue.push({ id, cb });
    return id;
  };
  window.cancelAnimationFrame = (id) => {
    __queue = __queue.filter((entry) => entry.id !== id);
  };

  // --- State -----------------------------------------------------------
  window.__frameIndex = 0;
  window.__trace = [];
  window.__settled = false;

  let __gesture = null; // { deltas, cumulative, stepIndex, pointerDownSent, pointerUpSent, done }
  let __scrollerEl = null;
  let __settledStreak = 0;
  let __lastScrollLeft = null;

  function getScroller() {
    if (__scrollerEl && document.contains(__scrollerEl)) return __scrollerEl;
    __scrollerEl =
      document.querySelector("blossom-carousel") ||
      document.querySelector(".carousel");
    return __scrollerEl;
  }

  // --- Gesture replay ----------------------------------------------------
  window.__startGesture = async () => {
    const res = await fetch("./gesture-trace.json");
    const trace = await res.json();
    __gesture = {
      deltas: trace.drag_deltas_px,
      cumulative: 0,
      stepIndex: 0, // index into deltas for the NEXT pointermove
      pointerDownSent: false,
      pointerUpSent: false,
      done: false,
    };
    __settledStreak = 0;
    __lastScrollLeft = null;
  };

  function dispatchPointer(type, clientX) {
    const el = getScroller();
    if (!el) return;
    const evt = new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
      buttons: type === "pointerup" ? 0 : 1,
      clientX,
      clientY: START_CLIENT_Y,
    });
    el.dispatchEvent(evt);
  }

  // One __step() worth of gesture advancement: pointerdown on the first
  // call after __startGesture(), then one pointermove per remaining delta,
  // then a single pointerup once the deltas are exhausted. No-op once done
  // (or if __startGesture() was never called) so callers can keep stepping
  // the rAF queue to settle.
  function advanceGesture() {
    if (!__gesture || __gesture.done) return;

    if (!__gesture.pointerDownSent) {
      dispatchPointer("pointerdown", START_CLIENT_X);
      __gesture.pointerDownSent = true;
      return;
    }

    if (__gesture.stepIndex < __gesture.deltas.length) {
      __gesture.cumulative += __gesture.deltas[__gesture.stepIndex];
      __gesture.stepIndex += 1;
      dispatchPointer("pointermove", START_CLIENT_X + __gesture.cumulative);
      return;
    }

    if (!__gesture.pointerUpSent) {
      dispatchPointer("pointerup", START_CLIENT_X + __gesture.cumulative);
      __gesture.pointerUpSent = true;
      __gesture.done = true;
    }
  }

  function flushRaf(now) {
    const q = __queue;
    __queue = [];
    q.forEach((entry) => entry.cb(now));
  }

  function readTransformMatrix(el) {
    const t = getComputedStyle(el).transform;
    try {
      return new DOMMatrixReadOnly(t === "none" || !t ? "" : t);
    } catch {
      return new DOMMatrixReadOnly();
    }
  }

  function captureFrame(idx) {
    const scroller = getScroller();
    const cards = [...document.querySelectorAll(".card")].map((c) => {
      const r = c.getBoundingClientRect();
      const cs = getComputedStyle(c);
      const m = readTransformMatrix(c);
      return {
        left: r.left,
        top: r.top,
        width: r.width,
        height: r.height,
        scale: m.a,
        opacity: parseFloat(cs.opacity),
      };
    });
    window.__trace.push({
      i: idx,
      scrollLeft: scroller ? scroller.scrollLeft : 0,
      cards,
    });
  }

  function updateSettled() {
    const scroller = getScroller();
    const sl = scroller ? scroller.scrollLeft : 0;
    const pointerUpSent = !!(__gesture && __gesture.pointerUpSent);
    if (!pointerUpSent) {
      __settledStreak = 0;
      __lastScrollLeft = sl;
      return;
    }
    if (__lastScrollLeft !== null && Math.abs(sl - __lastScrollLeft) < SETTLE_EPSILON) {
      __settledStreak += 1;
    } else {
      __settledStreak = 0;
    }
    __lastScrollLeft = sl;
    if (__settledStreak >= SETTLE_STREAK_TARGET) window.__settled = true;
  }

  window.__step = () => {
    __t += FRAME_MS;
    // Input events before rAF, matching real-browser event ordering.
    advanceGesture();
    flushRaf(__t);
    const idx = window.__frameIndex;
    captureFrame(idx);
    updateSettled();
    window.__frameIndex += 1;
    return idx;
  };

  window.__getTrace = () => JSON.stringify(window.__trace);

  window.__markReady = () => {
    // 1x1px, not display:none/visibility:hidden — `browse wait <sel>` uses
    // Playwright's default `state: "visible"`, which requires a non-empty
    // bounding box. A bare zero-content <div> collapses to height:0 (no
    // intrinsic content, no CSS applied) and Playwright never considers it
    // "visible", so `wait "#ready"` hangs to its 15s timeout even though
    // the element is attached to the DOM the whole time.
    const d = document.createElement("div");
    d.id = "ready";
    d.style.width = "1px";
    d.style.height = "1px";
    document.body.appendChild(d);
  };
})();
