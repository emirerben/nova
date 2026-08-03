/**
 * @jest-environment jsdom
 * @jest-environment-options {"url": "https://www.usekria.com/plan/items/x"}
 *
 * Relay-fallback gating on a PRODUCTION origin (not localhost).
 *
 * A mid-PUT network failure surfaces as a fetch TypeError / XHR error — the
 * same signature as a CORS block. The relay can only succeed for small bodies
 * (the Next proxy buffers in memory; Vercel caps request bodies ~4.5MB), so
 * large files must get a clear retryable error instead of a doomed silent
 * re-upload. Companion localhost behavior lives in plan/uploadToGcsRelay.test.ts.
 */

import {
  uploadContentTypeForFile,
  uploadToGcs,
  uploadToGcsWithProgress,
} from "@/lib/plan-api";

const SIGNED = "https://storage.googleapis.com/test-bucket/users/u1/clip.mp4?sig=abc";
const INTERRUPTED = "Upload interrupted. Check your connection and retry.";

function fileOfSize(bytes: number, name = "clip.mp4", type = "video/mp4"): File {
  const f = new File(["x"], name, { type });
  Object.defineProperty(f, "size", { value: bytes });
  return f;
}

class FakeXHR {
  static instances: FakeXHR[] = [];
  upload = { addEventListener: jest.fn() };
  listeners: Record<string, Array<() => void>> = {};
  status = 0;
  open = jest.fn();
  setRequestHeader = jest.fn();
  send = jest.fn();
  abort = jest.fn();
  constructor() {
    FakeXHR.instances.push(this);
  }
  addEventListener(name: string, fn: () => void) {
    (this.listeners[name] ??= []).push(fn);
  }
  emit(name: string) {
    (this.listeners[name] ?? []).forEach((fn) => fn());
  }
}

const RealXHR = global.XMLHttpRequest;

afterEach(() => {
  jest.restoreAllMocks();
  global.XMLHttpRequest = RealXHR;
  FakeXHR.instances.length = 0;
});

describe("uploadToGcs (fetch) relay gating on a production origin", () => {
  it("large file + network TypeError → clear retryable error, relay NOT called", async () => {
    global.fetch = jest.fn(async (url: RequestInfo | URL) => {
      if (String(url) === SIGNED) throw new TypeError("Failed to fetch");
      throw new Error("relay must not be called");
    }) as jest.Mock;

    await expect(uploadToGcs(SIGNED, fileOfSize(200 * 1024 * 1024))).rejects.toThrow(INTERRUPTED);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("small file (≤4MB) + network TypeError → relay still fires", async () => {
    const calls: string[] = [];
    global.fetch = jest.fn(async (url: RequestInfo | URL) => {
      calls.push(String(url));
      if (String(url) === SIGNED) throw new TypeError("Failed to fetch");
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    }) as jest.Mock;

    await uploadToGcs(SIGNED, fileOfSize(1024));
    expect(calls).toEqual([SIGNED, "/api/plan/uploads/relay"]);
  });
});

describe("uploadToGcsWithProgress (XHR) relay gating on a production origin", () => {
  it("large file + XHR network error → clear retryable error, relay NOT called", async () => {
    global.XMLHttpRequest = FakeXHR as unknown as typeof XMLHttpRequest;
    global.fetch = jest.fn(async () => {
      throw new Error("relay must not be called");
    }) as jest.Mock;

    const p = uploadToGcsWithProgress(SIGNED, fileOfSize(200 * 1024 * 1024), () => {});
    FakeXHR.instances[0].emit("error");
    await expect(p).rejects.toThrow(INTERRUPTED);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("small file + XHR network error → relay fires and receives the AbortSignal", async () => {
    global.XMLHttpRequest = FakeXHR as unknown as typeof XMLHttpRequest;
    const relayCalls: Array<{ url: string; init?: RequestInit }> = [];
    global.fetch = jest.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      relayCalls.push({ url: String(url), init });
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    }) as jest.Mock;

    const controller = new AbortController();
    const p = uploadToGcsWithProgress(SIGNED, fileOfSize(1024), () => {}, controller.signal);
    FakeXHR.instances[0].emit("error");
    await p;

    expect(relayCalls).toHaveLength(1);
    expect(relayCalls[0].url).toBe("/api/plan/uploads/relay");
    // Outside-voice #7: cancel must be able to stop an in-flight relay too.
    expect(relayCalls[0].init?.signal).toBe(controller.signal);
  });
});

describe("uploadContentTypeForFile — single source for sign + PUT", () => {
  it("prefers the browser-reported type", () => {
    expect(uploadContentTypeForFile(new File([""], "a.mp4", { type: "video/quicktime" }))).toBe(
      "video/quicktime",
    );
  });

  it("falls back by extension when file.type is empty (DJI/Files-app .mov case)", () => {
    expect(uploadContentTypeForFile(new File([""], "DJI_0001.MOV", { type: "" }))).toBe(
      "video/quicktime",
    );
    expect(uploadContentTypeForFile(new File([""], "shot.HEIC", { type: "" }))).toBe("image/heic");
    expect(uploadContentTypeForFile(new File([""], "pic.jpg", { type: "" }))).toBe("image/jpeg");
    expect(uploadContentTypeForFile(new File([""], "clip.unknown", { type: "" }))).toBe(
      "video/mp4",
    );
  });
});
