/**
 * Shared jsdom mocks for pocket-editor tests (mobile editor Lane A).
 *
 *  - installVisualViewportMock(): minimal EventTarget-based window.visualViewport
 *    stub so useKeyboardOffset-style hooks can be driven from tests.
 *  - installPointerEventPolyfill(): jsdom 20 has no PointerEvent; the
 *    HeroOverlayEditor pattern (extends MouseEvent so clientX/clientY/button
 *    survive) + no-op pointer-capture methods on Element.prototype if missing.
 */

export interface VisualViewportMockHandle {
  /** Update the stub's height (+ optional offsetTop) and dispatch "resize". */
  setHeight(h: number, offsetTop?: number): void;
  /** Remove the stub, restoring whatever window.visualViewport was before. */
  restore(): void;
}

export function installVisualViewportMock(): VisualViewportMockHandle {
  const win = window as unknown as Record<string, unknown>;
  const originalDescriptor = Object.getOwnPropertyDescriptor(window, "visualViewport");

  class VisualViewportStub extends EventTarget {
    width = window.innerWidth;
    height = window.innerHeight;
    offsetLeft = 0;
    offsetTop = 0;
    pageLeft = 0;
    pageTop = 0;
    scale = 1;
    onresize: ((ev: Event) => unknown) | null = null;
    onscroll: ((ev: Event) => unknown) | null = null;
  }

  const stub = new VisualViewportStub();
  Object.defineProperty(window, "visualViewport", {
    value: stub,
    configurable: true,
    writable: true,
  });

  return {
    setHeight(h: number, offsetTop = 0) {
      stub.height = h;
      stub.offsetTop = offsetTop;
      stub.dispatchEvent(new Event("resize"));
    },
    restore() {
      if (originalDescriptor) {
        Object.defineProperty(window, "visualViewport", originalDescriptor);
      } else {
        delete win.visualViewport;
      }
    },
  };
}

/**
 * jsdom 20 has no PointerEvent; @testing-library/dom then falls back to plain
 * Event, which drops clientX/clientY/button. Extend MouseEvent so coordinates
 * survive fireEvent.pointerDown/pointerUp. Returns a restore() function.
 */
export function installPointerEventPolyfill(): () => void {
  const win = window as unknown as Record<string, unknown>;
  const hadPointerEvent = "PointerEvent" in window;
  const originalPointerEvent = win.PointerEvent;

  class PointerEventPolyfill extends MouseEvent {
    pointerId: number;
    pointerType: string;
    isPrimary: boolean;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 0;
      this.pointerType = init.pointerType ?? "mouse";
      this.isPrimary = init.isPrimary ?? true;
    }
  }

  if (!hadPointerEvent) {
    win.PointerEvent = PointerEventPolyfill;
  }

  // jsdom doesn't implement pointer capture — install no-ops if missing.
  const proto = Element.prototype as unknown as Record<string, unknown>;
  const installedCaptureMethods: string[] = [];
  for (const name of ["setPointerCapture", "releasePointerCapture"]) {
    if (!(name in Element.prototype)) {
      Object.defineProperty(Element.prototype, name, {
        value: () => {},
        configurable: true,
        writable: true,
      });
      installedCaptureMethods.push(name);
    }
  }

  return function restore() {
    if (hadPointerEvent) win.PointerEvent = originalPointerEvent;
    else delete win.PointerEvent;
    for (const name of installedCaptureMethods) {
      delete proto[name];
    }
  };
}
