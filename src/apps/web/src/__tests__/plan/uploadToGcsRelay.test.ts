/**
 * Regression: browser→GCS PUT dies with "failed to fetch" (TypeError) on origins
 * the bucket's CORS config doesn't list (any localhost). uploadToGcs must fall
 * back to the API relay (/uploads/relay) instead of surfacing the error — this
 * covers clip, SFX, overlay-card, and voiceover uploads in one place.
 */

import { uploadToGcs } from "@/lib/plan-api";

const SIGNED = "https://storage.googleapis.com/test-bucket/users/u1/clip.mp4?sig=abc";

afterEach(() => {
  jest.restoreAllMocks();
});

it("falls back to the relay when the direct PUT fails at the network level", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  global.fetch = jest.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    if (String(url) === SIGNED) {
      // What a CORS-blocked fetch looks like: rejects with TypeError.
      throw new TypeError("Failed to fetch");
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
  }) as jest.Mock;

  const file = new File(["bytes"], "clip.mp4", { type: "video/mp4" });
  await uploadToGcs(SIGNED, file);

  expect(calls).toHaveLength(2);
  expect(calls[0].url).toBe(SIGNED);
  expect(calls[1].url).toBe("/api/plan/uploads/relay");
  const form = calls[1].init?.body as FormData;
  expect(form).toBeInstanceOf(FormData);
  expect(form.get("signed_url")).toBe(SIGNED);
  expect((form.get("file") as File).name).toBe("clip.mp4");
});

it("does NOT relay on an HTTP error from GCS (signed-URL problem, not CORS)", async () => {
  global.fetch = jest.fn(async (url: RequestInfo | URL) => {
    if (String(url) === SIGNED) {
      return { ok: false, status: 403, json: async () => ({}) } as Response;
    }
    throw new Error("relay must not be called");
  }) as jest.Mock;

  const file = new File(["bytes"], "clip.mp4", { type: "video/mp4" });
  await expect(uploadToGcs(SIGNED, file)).rejects.toThrow("Upload failed (403)");
  expect(global.fetch).toHaveBeenCalledTimes(1);
});
