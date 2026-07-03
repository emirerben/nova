import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import {
  DESKTOP_QUERY,
  FULL_QUERY,
  resolveLayoutMode,
  useEditorLayoutMode,
} from "../../../app/plan/items/[id]/_editor/useEditorLayoutMode";

class MockMediaQueryList {
  readonly media: string;
  matches: boolean;
  private listeners = new Set<(event: MediaQueryListEvent) => void>();

  constructor(media: string, matches: boolean) {
    this.media = media;
    this.matches = matches;
  }

  addEventListener(_: "change", listener: (event: MediaQueryListEvent) => void) {
    this.listeners.add(listener);
  }

  removeEventListener(_: "change", listener: (event: MediaQueryListEvent) => void) {
    this.listeners.delete(listener);
  }

  setMatches(matches: boolean) {
    this.matches = matches;
    const event = { matches, media: this.media } as MediaQueryListEvent;
    this.listeners.forEach((listener) => listener(event));
  }
}

describe("resolveLayoutMode", () => {
  it("maps desktop breakpoints to editor modes", () => {
    expect(resolveLayoutMode(true, true)).toBe("full");
    expect(resolveLayoutMode(false, true)).toBe("overlay");
    expect(resolveLayoutMode(false, false)).toBe("light");
  });
});

describe("useEditorLayoutMode", () => {
  const queries = new Map<string, MockMediaQueryList>();

  beforeEach(() => {
    queries.clear();
    queries.set(FULL_QUERY, new MockMediaQueryList(FULL_QUERY, true));
    queries.set(DESKTOP_QUERY, new MockMediaQueryList(DESKTOP_QUERY, true));
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: jest.fn((query: string) => {
        const mql = queries.get(query);
        if (!mql) throw new Error(`Unexpected query: ${query}`);
        return mql;
      }),
    });
  });

  it("switches full to overlay to light as matchMedia changes", () => {
    const { result } = renderHook(() => useEditorLayoutMode());

    expect(result.current).toBe("full");

    act(() => {
      queries.get(FULL_QUERY)?.setMatches(false);
    });
    expect(result.current).toBe("overlay");

    act(() => {
      queries.get(DESKTOP_QUERY)?.setMatches(false);
    });
    expect(result.current).toBe("light");
  });
});
