/**
 * Tests for plan/_components/AssetPool.tsx (overlay auto-placement PR0, plans/005).
 *
 * Covers:
 *   1. Flag off → renders nothing; flag on + no assets → serif empty state.
 *   2. Upload flow: upload-urls → GCS PUT → register; tile appears.
 *   3. deduped=true → no duplicate tile + "Already in your pool" notice.
 *   4. Cap: 20 assets → add affordance disabled + inline reason (not tooltip-only).
 *   5. Delete calls DELETE and removes the tile.
 *   6. status="failed" → quiet dashed-zinc failure tile, no red classes.
 *   7. Backend 404 (dual-flag trap) → "Visuals pool isn't available" line.
 *   8. Status polling: non-terminal asset → 5s refetch flips the tile in place;
 *      stops once every asset is terminal; never starts when all are terminal.
 *   9. Detected brands ride the subject line's title attribute.
 *
 * fetch is mocked at the global level so the plan-api URL contract is exercised.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import AssetPool from "@/app/plan/_components/AssetPool";

// jsdom lacks crypto.subtle — mock digest with a deterministic buffer.
beforeAll(() => {
  const subtle = {
    digest: jest.fn(async () => new Uint8Array(32).fill(0xab).buffer),
  };
  const existing = (globalThis as Record<string, unknown>).crypto ?? {};
  Object.defineProperty(globalThis, "crypto", {
    value: { ...(existing as object), subtle },
    configurable: true,
    writable: true,
  });
  // jsdom's File lacks arrayBuffer() in some versions.
  if (typeof File.prototype.arrayBuffer !== "function") {
    File.prototype.arrayBuffer = async function arrayBuffer() {
      return new ArrayBuffer(8);
    };
  }
});

const FLAG = "NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED";

function makeAsset(overrides: Record<string, unknown> = {}) {
  return {
    id: `asset-${Math.random().toString(36).slice(2)}`,
    kind: "image",
    status: "ready",
    source_filename: "shot.png",
    duration_s: null,
    aspect: null,
    subject: null,
    user_context: "",
    nova_description: null,
    nova_on_screen_text: null,
    display_url: "https://storage.example/signed/shot.png",
    deduped: false,
    gcs_path: "users/u1/plan/item-1/pool/shot.png",
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

/** fetch mock routing on (method, url, init) — returns undefined to fall through.
 *  Route handlers may THROW (e.g. a TypeError) to simulate a network/CORS fail. */
function mockFetch(
  routes: (method: string, url: string, init?: RequestInit) => Response | undefined,
) {
  const fn = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const res = routes(method, url, init);
    if (!res) throw new Error(`Unmocked fetch: ${method} ${url}`);
    return res;
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

/** Standard happy-path route table; override individual handlers per test. */
function listRoute(assets: unknown[], maxAssets = 20) {
  return (method: string, url: string) =>
    method === "GET" && url === "/api/plan/plan-items/item-1/assets"
      ? jsonResponse({ assets, max_assets: maxAssets })
      : undefined;
}

async function renderPool(
  props: Omit<React.ComponentProps<typeof AssetPool>, "itemId"> = {},
) {
  await act(async () => {
    render(<AssetPool itemId="item-1" {...props} />);
  });
}

afterEach(() => {
  jest.restoreAllMocks();
  delete process.env[FLAG];
});

describe("AssetPool — flag gating", () => {
  it("renders nothing when the flag is off", async () => {
    // Flag deliberately unset. No fetch should fire either.
    const fetchSpy = mockFetch(() => jsonResponse({ assets: [], max_assets: 20 }));
    const { container } = render(<AssetPool itemId="item-1" />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/visuals pool/i)).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders the serif empty state when flag on + no assets", async () => {
    process.env[FLAG] = "true";
    mockFetch(listRoute([]));
    await renderPool();
    expect(screen.getByText(/visuals pool/i)).toBeInTheDocument();
    // Empty state leads with the action (§9), never "Nothing here yet".
    expect(
      screen.getByText("Drop the screenshots you mention in your script"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add visuals/i })).toBeInTheDocument();
    expect(screen.queryByText(/nothing here/i)).toBeNull();
  });

  it("shows a durable saved receipt in the embedded Visuals tab", async () => {
    process.env[FLAG] = "true";
    mockFetch(
      listRoute([
        makeAsset({ id: "photo-1", kind: "image", source_filename: "photo.png" }),
        makeAsset({ id: "video-1", kind: "video", source_filename: "support.mov" }),
      ]),
    );
    await renderPool({ embedded: true });

    expect(screen.getByText("Photos and supporting videos")).toBeInTheDocument();
    expect(screen.getByTestId("visuals-saved-receipt")).toHaveTextContent(
      "2 visuals saved (1 photo, 1 video)",
    );
  });
});

describe("AssetPool — upload flow (presigned direct-PUT, R1/C9+C14)", () => {
  const UPLOAD_URLS_URL = "/api/plan/plan-items/item-1/assets/upload-urls";
  const REGISTER_URL = "/api/plan/plan-items/item-1/assets";
  const SIGNED_PUT = "https://storage.googleapis.com/bucket/users/u1/plan/item-1/pool/shot.png";
  const signedTarget = (gcsPath: string, uploadUrl: string, clientUploadId: string) => ({
    reservation_id: `reservation-${gcsPath.split("/").at(-1)}`,
    client_upload_id: clientUploadId,
    upload_url: uploadUrl,
    gcs_path: gcsPath,
    expires_at: "2026-08-14T10:00:00Z",
    upload_headers: { "x-goog-if-generation-match": "0" },
  });

  it("upload-urls → direct GCS PUT → register; tile appears; NO proxy body cap", async () => {
    process.env[FLAG] = "true";
    const onMutated = jest.fn();
    const registered = makeAsset({ subject: "settings toggle" });
    let putBody: unknown = null;
    let putHeaders: HeadersInit | undefined;
    let registerBody: Record<string, unknown> | null = null;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === "/api/plan/plan-items/item-1/assets") {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/shot.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        putBody = init?.body;
        putHeaders = init?.headers;
        return jsonResponse({}, 200);
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerBody = JSON.parse((init?.body as string) ?? "{}");
        return jsonResponse(registered);
      }
      return undefined;
    });
    await renderPool({ onMutated });

    const input = screen.getByLabelText(/add visuals to your pool/i);
    const file = new File(["png-bytes"], "shot.png", { type: "image/png" });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(screen.getByText("settings toggle")).toBeInTheDocument();
    });
    expect(onMutated).toHaveBeenCalledTimes(1);

    // The bytes went straight to GCS via a direct PUT — never buffered through
    // the Next api-proxy multipart upload (that's the Vercel 4.5MB cap path).
    const fetchMock = global.fetch as jest.Mock;
    expect(fetchMock.mock.calls.some(([u]) => String(u) === SIGNED_PUT)).toBe(true);
    expect(putBody).toBe(file);
    expect(putHeaders).toMatchObject({
      "Content-Type": "image/png",
      "x-goog-if-generation-match": "0",
    });
    // The legacy one-shot multipart proxy is NOT used.
    expect(
      fetchMock.mock.calls.some(
        ([u]) => String(u) === "/api/plan/plan-items/item-1/assets/upload",
      ),
    ).toBe(false);
    // Register carries the gcs_path + a client-computed content_hash for dedupe.
    expect(registerBody!.gcs_path).toBe("users/u1/plan/item-1/pool/shot.png");
    expect(registerBody!.reservation_id).toBe("reservation-shot.png");
    expect(registerBody!.content_type).toBe("image/png");
    expect(registerBody!.source_filename).toBe("shot.png");
    expect(typeof registerBody!.content_hash).toBe("string");
  });

  it("updates the embedded saved receipt only after registration succeeds", async () => {
    process.env[FLAG] = "true";
    const registered = makeAsset({ source_filename: "saved.png", subject: "saved photo" });
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/saved.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({}, 200);
      if (method === "POST" && url === REGISTER_URL) return jsonResponse(registered);
      return undefined;
    });
    await renderPool({ embedded: true });
    expect(screen.getByTestId("visuals-saved-receipt")).toHaveTextContent("No visuals saved");

    fireEvent.change(screen.getByLabelText("Add visuals to your pool (embedded)"), {
      target: { files: [new File(["png"], "saved.png", { type: "image/png" })] },
    });

    await waitFor(() =>
      expect(screen.getByTestId("visuals-saved-receipt")).toHaveTextContent(
        "1 visual saved",
      ),
    );
  });

  it("relays the signed PUT through /uploads/relay on a CORS TypeError (localhost)", async () => {
    process.env[FLAG] = "true";
    const registered = makeAsset({ subject: "relayed" });
    let relayed = false;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/shot.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        // Simulate the bucket-CORS failure: fetch throws a TypeError.
        throw new TypeError("Failed to fetch");
      }
      if (method === "POST" && url === "/api/plan/uploads/relay") {
        relayed = true;
        expect(init?.headers).toMatchObject({
          "X-Correlation-Id": expect.stringMatching(/^batch-/),
        });
        expect(init?.body).toBeInstanceOf(FormData);
        const form = init?.body as FormData;
        expect(form.get("file_size_bytes")).toBe(String(file.size));
        expect(form.get("if_generation_match")).toBe("0");
        return jsonResponse({ ok: true });
      }
      if (method === "POST" && url === REGISTER_URL) {
        return jsonResponse(registered);
      }
      return undefined;
    });
    await renderPool();

    const input = screen.getByLabelText(/add visuals to your pool/i);
    const file = new File(["png-bytes"], "shot.png", { type: "image/png" });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(screen.getByText("relayed")).toBeInTheDocument();
    });
    expect(relayed).toBe(true);
  });

  it("signs each file independently and never runs more than three upload pipelines", async () => {
    process.env[FLAG] = "true";
    let presignCalls = 0;
    let activePuts = 0;
    let maxActivePuts = 0;
    let registerCalls = 0;
    const releases: Array<() => void> = [];
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        presignCalls += 1;
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ filename: string; client_upload_id: string }>;
        };
        expect(body.files).toHaveLength(1);
        expect(body.files[0].client_upload_id.startsWith("file-")).toBe(true);
        return jsonResponse({
          urls: [
            signedTarget(
              `users/u1/plan/item-1/pool/${body.files[0].filename}`,
              `https://storage.example/${body.files[0].filename}`,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url.startsWith("https://storage.example/")) {
        activePuts += 1;
        maxActivePuts = Math.max(maxActivePuts, activePuts);
        await new Promise<void>((resolve) => {
          releases.push(() => {
            activePuts -= 1;
            resolve();
          });
        });
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerCalls += 1;
        const body = JSON.parse(String(init?.body)) as { source_filename: string };
        return jsonResponse(
          makeAsset({
            id: `asset-${body.source_filename}`,
            source_filename: body.source_filename,
          }),
        );
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    await renderPool();
    const files = ["one.png", "two.png", "three.png", "four.png"].map(
      (name) => new File([name], name, { type: "image/png" }),
    );
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files },
    });

    await waitFor(() => expect(releases).toHaveLength(3));
    expect(presignCalls).toBe(3);
    expect(maxActivePuts).toBe(3);
    await act(async () => releases[0]());
    await waitFor(() => expect(releases).toHaveLength(4));
    expect(presignCalls).toBe(4);
    await waitFor(() => expect(registerCalls).toBe(1));
    expect(maxActivePuts).toBe(3);
    await act(async () => releases.slice(1).forEach((release) => release()));
    await waitFor(() => expect(screen.getByText("4 of 4 added.")).toBeInTheDocument());
  });

  it("clamps selection to remaining capacity before requesting upload URLs", async () => {
    process.env[FLAG] = "true";
    const existing = Array.from({ length: 19 }, (_, index) =>
      makeAsset({ id: `asset-${index}`, source_filename: `asset-${index}.png` }),
    );
    let signedFiles = 0;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: existing, max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        signedFiles = body.files.length;
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/only.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({});
      if (method === "POST" && url === REGISTER_URL) return jsonResponse(makeAsset());
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: ["one.png", "two.png", "three.png"].map(
          (name) => new File([name], name, { type: "image/png" }),
        ),
      },
    });
    await waitFor(() => expect(signedFiles).toBe(1));
    expect(screen.getByText("Your pool has room for 1 more visual. Select up to 1.")).toBeInTheDocument();
  });

  it("rejects unknown empty-MIME extensions before networking", async () => {
    process.env[FLAG] = "true";
    const fetchMock = mockFetch(listRoute([]));
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["pdf"], "notes.pdf", { type: "" })] },
    });
    expect(
      screen.getByText(/export them as JPG, PNG, WebP, HEIC\/HEIF, MP4, or MOV/i),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url) === UPLOAD_URLS_URL && (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(false);
  });

  it("replaces a presign 500 with actionable copy and a stage-specific retry", async () => {
    process.env[FLAG] = "true";
    let presignCalls = 0;
    const correlationIds: string[] = [];
    const clientUploadIds: string[] = [];
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        presignCalls += 1;
        const headers = init?.headers as Record<string, string>;
        correlationIds.push(headers["X-Correlation-Id"]);
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        clientUploadIds.push(body.files[0].client_upload_id);
        if (presignCalls === 1) return jsonResponse({ detail: "Internal Server Error" }, 500);
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/retry.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({});
      if (method === "POST" && url === REGISTER_URL) return jsonResponse(makeAsset());
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["retry"], "retry.png", { type: "image/png" })] },
    });
    expect(
      await screen.findByText("Kria couldn’t start this upload. Retry in a moment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/internal server error/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("1 of 1 added.")).toBeInTheDocument());
    expect(presignCalls).toBe(2);
    expect(new Set(correlationIds).size).toBe(1);
    expect(new Set(clientUploadIds).size).toBe(1);
  });

  it("preserves an actionable terminal 4xx and does not offer a futile retry", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        return jsonResponse(
          {
            detail: "This upload retry does not match the originally selected file.",
            code: "reservation_mismatch",
            retryable: false,
          },
          409,
        );
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["x"], "shot.png", { type: "image/png" })] },
    });

    expect(
      await screen.findByText("Kria couldn’t start this upload. Retry in a moment."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("keeps mixed partial successes and summarizes only the failed file", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ filename: string; client_upload_id: string }>;
        };
        return jsonResponse({
          urls: body.files.map((file) =>
            signedTarget(
              `users/u1/plan/item-1/pool/${file.filename}`,
              `https://storage.example/${file.filename}`,
              file.client_upload_id,
            ),
          ),
        });
      }
      if (method === "PUT" && url === "https://storage.example/bad.png") {
        return jsonResponse({}, 503);
      }
      if (method === "PUT" && url.startsWith("https://storage.example/")) {
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        const body = JSON.parse(String(init?.body)) as { source_filename: string };
        return jsonResponse(
          makeAsset({ id: body.source_filename, source_filename: body.source_filename }),
        );
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: ["good-a.png", "bad.png", "good-b.png"].map(
          (name) => new File([name], name, { type: "image/png" }),
        ),
      },
    });

    expect(await screen.findByText("2 of 3 added; 1 needs attention.")).toBeInTheDocument();
    expect(screen.getByText("Upload interrupted. Check your connection and retry.")).toBeInTheDocument();
  });

  it("retries registration without uploading the file a second time", async () => {
    process.env[FLAG] = "true";
    let putCalls = 0;
    let registerCalls = 0;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/register.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        putCalls += 1;
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerCalls += 1;
        return registerCalls === 1
          ? jsonResponse({ detail: "Internal Server Error" }, 500)
          : jsonResponse(makeAsset({ source_filename: "register.png" }));
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["register"], "register.png", { type: "image/png" })] },
    });
    expect(
      await screen.findByText("The file uploaded, but Kria couldn’t add it to your visuals."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("1 of 1 added.")).toBeInTheDocument());
    expect(putCalls).toBe(1);
    expect(registerCalls).toBe(2);
  });

  it("leaves registration failure actions available for an independent retry", async () => {
    process.env[FLAG] = "true";
    let registerCalls = 0;
    let releaseSecond: (() => void) | null = null;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ filename: string; client_upload_id: string }>;
        };
        return jsonResponse({
          urls: body.files.map((file) =>
            signedTarget(
              `users/u1/plan/item-1/pool/${file.filename}`,
              `https://storage.example/registration-${file.filename}`,
              file.client_upload_id,
            ),
          ),
        });
      }
      if (method === "PUT" && url.startsWith("https://storage.example/registration-")) {
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerCalls += 1;
        const body = JSON.parse(String(init?.body)) as { source_filename: string };
        if (registerCalls === 1) return jsonResponse({ detail: "server" }, 500);
        if (registerCalls === 2) {
          await new Promise<void>((resolve) => {
            releaseSecond = resolve;
          });
        }
        return jsonResponse(makeAsset({ source_filename: body.source_filename }));
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: ["retry-a.png", "lane-b.png"].map(
          (name) => new File([name], name, { type: "image/png" }),
        ),
      },
    });
    expect(
      await screen.findByText("The file uploaded, but Kria couldn’t add it to your visuals."),
    ).toBeInTheDocument();
    await waitFor(() => expect(registerCalls).toBe(2));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.queryByRole("button", { name: "Remove retry-a.png" })).toBeNull();
    expect(screen.getByLabelText("Adding… retry-a.png")).toBeInTheDocument();
    await act(async () => releaseSecond?.());
    await waitFor(() => expect(screen.getByText("2 of 2 added.")).toBeInTheDocument());
    expect(registerCalls).toBe(3);
  });

  it("keeps a removed signed reservation counted until its signed lifetime ends", async () => {
    process.env[FLAG] = "true";
    let presignCalls = 0;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 1 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        presignCalls += 1;
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            {
              ...signedTarget(
                "users/u1/plan/item-1/pool/held.png",
                SIGNED_PUT,
                body.files[0].client_upload_id,
              ),
              expires_at: new Date(Date.now() + 60_000).toISOString(),
            },
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({}, 503);
      return undefined;
    });
    await renderPool();
    const input = screen.getByLabelText(/add visuals to your pool/i);
    fireEvent.change(input, {
      target: { files: [new File(["held"], "held.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("Upload interrupted. Check your connection and retry.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByText("held.png")).toBeNull();
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();
    expect(
      screen.getByText(
        "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.",
      ),
    ).toBeInTheDocument();
    expect(presignCalls).toBe(1);
  });

  it("conservatively holds capacity when a presign response is lost after server commit", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 1 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        throw new TypeError("response stream lost");
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["lost"], "lost.png", { type: "image/png" })] },
    });
    expect(
      await screen.findByText("Kria couldn’t start this upload. Retry in a moment."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();
    expect(
      screen.getByText(
        "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps registration-failure capacity for a full TTL even when the old target expires", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 1 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            {
              ...signedTarget(
                "users/u1/plan/item-1/pool/promoting.png",
                SIGNED_PUT,
                body.files[0].client_upload_id,
              ),
              expires_at: new Date(Date.now() - 1).toISOString(),
            },
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({});
      if (method === "POST" && url === REGISTER_URL) return jsonResponse({ detail: "server" }, 500);
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: [new File(["promoting"], "promoting.png", { type: "image/png" })],
      },
    });
    expect(
      await screen.findByText("The file uploaded, but Kria couldn’t add it to your visuals."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();
  });

  it("restores hidden capacity from the authoritative list after reload", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) =>
      method === "GET" && url === REGISTER_URL
        ? jsonResponse({
            assets: [],
            max_assets: 1,
            occupied_assets: 1,
            active_reservations: [
              {
                reservation_id: "reservation-from-server",
                release_at: new Date(Date.now() + 60_000).toISOString(),
              },
            ],
          })
        : undefined,
    );
    await renderPool();
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add visuals" })).toBeDisabled();
    expect(
      screen.getByText(
        "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.",
      ),
    ).toBeInTheDocument();
  });

  it("limits a new batch against authoritative hidden reservations before networking", async () => {
    process.env[FLAG] = "true";
    let requestedFiles = 0;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({
          assets: [],
          max_assets: 20,
          occupied_assets: 10,
          active_reservations: Array.from({ length: 10 }, (_, index) => ({
            reservation_id: `server-reservation-${index}`,
            release_at: new Date(Date.now() + 60_000).toISOString(),
          })),
        });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as { files: unknown[] };
        requestedFiles += body.files.length;
        return jsonResponse({ detail: "temporary" }, 503);
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: Array.from(
          { length: 20 },
          (_, index) => new File([`${index}`], `shot-${index}.png`, { type: "image/png" }),
        ),
      },
    });
    await waitFor(() => expect(requestedFiles).toBe(10));
    expect(
      screen.getByText("Your pool has room for 10 more visuals. Select up to 10."),
    ).toBeInTheDocument();
  });

  it("polls a cleanup-pending hold until maintenance releases it", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method !== "GET" || url !== REGISTER_URL) return undefined;
      listCalls += 1;
      return listCalls === 1
        ? jsonResponse({
            assets: [],
            max_assets: 1,
            occupied_assets: 1,
            active_reservations: [
              { reservation_id: "cleanup-pending", release_at: null },
            ],
          })
        : jsonResponse({
            assets: [],
            max_assets: 1,
            occupied_assets: 0,
            active_reservations: [],
          });
    });
    await renderPool();
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(5_000);
    });
    expect(listCalls).toBe(2);
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeEnabled();
  });

  it("refreshes the signed target and retransfers after an interrupted PUT", async () => {
    process.env[FLAG] = "true";
    let presignCalls = 0;
    let putCalls = 0;
    let registerCalls = 0;
    const clientIds: string[] = [];
    const correlationIds: string[] = [];
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        presignCalls += 1;
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        clientIds.push(body.files[0].client_upload_id);
        correlationIds.push((init?.headers as Record<string, string>)["X-Correlation-Id"]);
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/interrupted.png",
              `https://storage.example/interrupted-${presignCalls}`,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url.startsWith("https://storage.example/interrupted-")) {
        putCalls += 1;
        return putCalls === 1 ? jsonResponse({}, 503) : jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerCalls += 1;
        return jsonResponse(makeAsset({ source_filename: "interrupted.png" }));
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["retry"], "interrupted.png", { type: "image/png" })] },
    });
    expect(
      await screen.findByText("Upload interrupted. Check your connection and retry."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("1 of 1 added.")).toBeInTheDocument());
    expect({ presignCalls, putCalls, registerCalls }).toEqual({
      presignCalls: 2,
      putCalls: 2,
      registerCalls: 1,
    });
    expect(new Set(clientIds).size).toBe(1);
    expect(new Set(correlationIds).size).toBe(1);
  });

  it("restarts transfer when registration reports an expired reservation", async () => {
    process.env[FLAG] = "true";
    let presignCalls = 0;
    let putCalls = 0;
    let registerCalls = 0;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        presignCalls += 1;
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/expired.png",
              `https://storage.example/expired-${presignCalls}`,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url.startsWith("https://storage.example/expired-")) {
        putCalls += 1;
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerCalls += 1;
        return registerCalls === 1
          ? jsonResponse(
              {
                detail: "This upload expired. Upload the file again.",
                code: "upload_reservation_expired",
                stage: "transfer",
                retryable: true,
              },
              404,
            )
          : jsonResponse(makeAsset({ source_filename: "expired.png" }));
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["expired"], "expired.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("Upload interrupted. Check your connection and retry.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("1 of 1 added.")).toBeInTheDocument());
    expect({ presignCalls, putCalls, registerCalls }).toEqual({
      presignCalls: 2,
      putCalls: 2,
      registerCalls: 2,
    });
  });

  it("rejects a mismatched signed target identity before any transfer", async () => {
    process.env[FLAG] = "true";
    let putCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        return jsonResponse({
          urls: [signedTarget("users/u1/plan/item-1/pool/wrong.png", SIGNED_PUT, "wrong-id")],
        });
      }
      if (method === "PUT") {
        putCalls += 1;
        return jsonResponse({});
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["wrong"], "wrong.png", { type: "image/png" })] },
    });
    expect(
      await screen.findByText("Kria couldn’t start this upload. Retry in a moment."),
    ).toBeInTheDocument();
    expect(putCalls).toBe(0);
  });

  it("rejects a signed-target count mismatch before any transfer", async () => {
    process.env[FLAG] = "true";
    let putCalls = 0;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        return jsonResponse({ urls: [] });
      }
      if (method === "PUT") {
        putCalls += 1;
        return jsonResponse({});
      }
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: ["one.png", "two.png"].map(
          (name) => new File([name], name, { type: "image/png" }),
        ),
      },
    });
    expect(
      await screen.findAllByText("Kria couldn’t start this upload. Retry in a moment."),
    ).toHaveLength(2);
    expect(putCalls).toBe(0);
  });

  it("runs independent registration work within the bounded upload pipelines", async () => {
    process.env[FLAG] = "true";
    let registerCalls = 0;
    let activeRegistrations = 0;
    let maxActiveRegistrations = 0;
    let releaseFirst: (() => void) | null = null;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ filename: string; client_upload_id: string }>;
        };
        return jsonResponse({
          urls: body.files.map((file) =>
            signedTarget(
              `users/u1/plan/item-1/pool/${file.filename}`,
              `https://storage.example/lane-${file.filename}`,
              file.client_upload_id,
            ),
          ),
        });
      }
      if (method === "PUT" && url.startsWith("https://storage.example/lane-")) {
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerCalls += 1;
        activeRegistrations += 1;
        maxActiveRegistrations = Math.max(maxActiveRegistrations, activeRegistrations);
        if (registerCalls === 1) {
          await new Promise<void>((resolve) => {
            releaseFirst = resolve;
          });
        }
        activeRegistrations -= 1;
        const body = JSON.parse(String(init?.body)) as { source_filename: string };
        return jsonResponse(makeAsset({ source_filename: body.source_filename }));
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: {
        files: ["lane-a.png", "lane-b.png"].map(
          (name) => new File([name], name, { type: "image/png" }),
        ),
      },
    });
    await waitFor(() => expect(registerCalls).toBe(2));
    expect(maxActiveRegistrations).toBe(2);
    await act(async () => releaseFirst?.());
    await waitFor(() => expect(screen.getByText("2 of 2 added.")).toBeInTheDocument());
    expect(registerCalls).toBe(2);
    expect(maxActiveRegistrations).toBe(2);
  });

  it("does not let stale initial hydration erase a newly registered asset", async () => {
    process.env[FLAG] = "true";
    let releaseInitial: ((value: Response) => void) | null = null;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url === REGISTER_URL) {
        return await new Promise<Response>((resolve) => {
          releaseInitial = resolve;
        });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/kept.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({});
      if (method === "POST" && url === REGISTER_URL) {
        return jsonResponse(makeAsset({ subject: "kept after hydration" }));
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    render(<AssetPool itemId="item-1" />);
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["kept"], "kept.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("kept after hydration")).toBeInTheDocument();
    await act(async () => releaseInitial?.(jsonResponse({ assets: [], max_assets: 20 })));
    expect(screen.getByText("kept after hydration")).toBeInTheDocument();
  });

  it("deduped=true → no duplicate tile + quiet notice", async () => {
    process.env[FLAG] = "true";
    const existing = makeAsset({ id: "asset-existing", subject: "dashboard" });
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [existing], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/dup.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        return jsonResponse({}, 200);
      }
      if (method === "POST" && url === REGISTER_URL) {
        return jsonResponse({ ...existing, deduped: true });
      }
      return undefined;
    });
    await renderPool();
    expect(screen.getByText("dashboard")).toBeInTheDocument();

    const input = screen.getByLabelText(/add visuals to your pool/i);
    const file = new File(["same-bytes"], "dup.png", { type: "image/png" });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(screen.getByText("Already in your pool")).toBeInTheDocument();
    });
    // Still exactly one tile for that asset.
    expect(screen.getAllByText("dashboard")).toHaveLength(1);
    expect(screen.getByText("1 of 20")).toBeInTheDocument();
  });

  it("upserts a deduped registration that was absent from the last list snapshot", async () => {
    process.env[FLAG] = "true";
    const remoteDuplicate = makeAsset({
      id: "asset-other-tab",
      source_filename: "other-tab.png",
      subject: "from another tab",
      deduped: true,
    });
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            signedTarget(
              "users/u1/plan/item-1/pool/other-tab.png",
              SIGNED_PUT,
              body.files[0].client_upload_id,
            ),
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) return jsonResponse({});
      if (method === "POST" && url === REGISTER_URL) return jsonResponse(remoteDuplicate);
      return undefined;
    });
    await renderPool();
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["same"], "other-tab.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("Already in your pool")).toBeInTheDocument();
    expect(screen.getByText("from another tab")).toBeInTheDocument();
    expect(screen.getByText("1 of 20")).toBeInTheDocument();
  });

  it("releases stale authoritative reservation capacity after dedupe", async () => {
    process.env[FLAG] = "true";
    const existing = makeAsset({ id: "asset-existing", subject: "existing visual" });
    const reservationId = "reservation-dedupe-race.png";
    let releaseList: ((response: Response) => void) | null = null;
    let releasePut: (() => void) | null = null;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url === REGISTER_URL) {
        return await new Promise<Response>((resolve) => {
          releaseList = resolve;
        });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        const body = JSON.parse(String(init?.body)) as {
          files: Array<{ client_upload_id: string }>;
        };
        return jsonResponse({
          urls: [
            {
              ...signedTarget(
                "users/u1/plan/item-1/pool/dedupe-race.png",
                SIGNED_PUT,
                body.files[0].client_upload_id,
              ),
              reservation_id: reservationId,
            },
          ],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        await new Promise<void>((resolve) => {
          releasePut = resolve;
        });
        return jsonResponse({});
      }
      if (method === "POST" && url === REGISTER_URL) {
        return jsonResponse({ ...existing, deduped: true });
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    render(<AssetPool itemId="item-1" />);
    fireEvent.change(screen.getByLabelText(/add visuals to your pool/i), {
      target: { files: [new File(["same"], "dedupe-race.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(releasePut).not.toBeNull());
    await act(async () =>
      releaseList?.(
        jsonResponse({
          assets: [existing],
          max_assets: 2,
          occupied_assets: 2,
          active_reservations: [
            {
              reservation_id: reservationId,
              release_at: new Date(Date.now() + 60_000).toISOString(),
            },
          ],
        }),
      ),
    );
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();
    await act(async () => releasePut?.());
    expect(await screen.findByText("Already in your pool")).toBeInTheDocument();
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeEnabled();
  });
});

describe("AssetPool — cap", () => {
  it("20 assets → add affordance disabled with inline reason", async () => {
    process.env[FLAG] = "true";
    const assets = Array.from({ length: 20 }, (_, i) =>
      makeAsset({ id: `asset-${i}`, source_filename: `shot-${i}.png` }),
    );
    mockFetch(listRoute(assets));
    await renderPool();

    expect(screen.getByText("20 of 20")).toBeInTheDocument();
    const addButton = screen.getByRole("button", { name: "Add" });
    expect(addButton).toBeDisabled();
    // Inline reason text, never tooltip-only.
    expect(
      screen.getByText(/pool is full — remove a visual to add another/i),
    ).toBeInTheDocument();
  });
});

describe("AssetPool — delete", () => {
  it("calls DELETE and removes the tile", async () => {
    process.env[FLAG] = "true";
    const onMutated = jest.fn();
    const asset = makeAsset({ id: "asset-del", source_filename: "gone.png", subject: "toggle" });
    let deleteCalled = false;
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/plan/plan-items/item-1/assets") {
        return jsonResponse({ assets: [asset], max_assets: 1, occupied_assets: 1 });
      }
      if (method === "DELETE" && url === "/api/plan/plan-items/item-1/assets/asset-del") {
        deleteCalled = true;
        return jsonResponse({ ok: true });
      }
      return undefined;
    });
    await renderPool({ onMutated });
    expect(screen.getByText("toggle")).toBeInTheDocument();
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeDisabled();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove gone.png" }));
    });

    expect(deleteCalled).toBe(true);
    expect(onMutated).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.queryByText("toggle")).toBeNull();
    });
    expect(screen.getByLabelText(/add visuals to your pool/i)).toBeEnabled();
  });
});

describe("AssetPool — failed asset", () => {
  it("renders the quiet dashed failure tile with no red", async () => {
    process.env[FLAG] = "true";
    const failed = makeAsset({
      id: "asset-fail",
      status: "failed",
      source_filename: "broken.heic",
      display_url: null,
    });
    mockFetch(listRoute([failed]));
    const { container } = await (async () => {
      let result: ReturnType<typeof render>;
      await act(async () => {
        result = render(<AssetPool itemId="item-1" />);
      });
      return result!;
    })();

    expect(screen.getByText(/couldn't read this file/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove broken.heic" })).toBeInTheDocument();
    // Quiet zinc, never red (2A state table).
    expect(container.innerHTML).not.toMatch(/red-\d|text-red|bg-red|border-red/);
  });

  it("POSTs the idempotent reanalysis endpoint and renders the queued state", async () => {
    process.env[FLAG] = "true";
    const failed = makeAsset({
      id: "asset-retry",
      status: "failed",
      source_filename: "retry.mov",
      display_url: null,
      error_detail: "Kria temporarily couldn't analyze this file. Try again.",
      retryable: true,
    });
    let reanalyzeCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/plan/plan-items/item-1/assets") {
        return jsonResponse({ assets: [failed], max_assets: 20 });
      }
      if (
        method === "POST" &&
        url === "/api/plan/plan-items/item-1/assets/asset-retry/reanalyze"
      ) {
        reanalyzeCalls += 1;
        return jsonResponse({ ...failed, status: "queued", error_detail: null });
      }
      return undefined;
    });
    await renderPool();
    fireEvent.click(screen.getByRole("button", { name: "Retry analysis" }));
    await waitFor(() => expect(screen.getByText("Queued…")).toBeInTheDocument());
    expect(reanalyzeCalls).toBe(1);
  });
});

describe("AssetPool — backend flag mismatch (dual-flag trap)", () => {
  it("surfaces the unavailable line on a backend 404", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) =>
      method === "GET" && url === "/api/plan/plan-items/item-1/assets"
        ? jsonResponse({ detail: "Auto-placement not available." }, 404)
        : undefined,
    );
    await renderPool();

    expect(
      screen.getByText("Visuals pool isn't available right now."),
    ).toBeInTheDocument();
    // Never silent, but also never a scary red banner.
    expect(screen.queryByText(/drop the screenshots/i)).toBeNull();
  });
});

describe("AssetPool — \u201cUse in edit\u201d promotion (pool asset \u2192 clip)", () => {
  it("renders the affordance on video assets and calls the handler with the asset", async () => {
    process.env[FLAG] = "true";
    const video = makeAsset({
      kind: "video",
      status: "ready",
      gcs_path: "users/u1/plan/item-1/pool/rec.mp4",
      source_filename: "rec.mp4",
    });
    mockFetch(listRoute([video]));
    const onUseInEdit = jest.fn();
    await act(async () => {
      render(<AssetPool itemId="item-1" attachedPaths={[]} onUseInEdit={onUseInEdit} />);
    });

    const btn = screen.getByRole("button", { name: /use rec\.mp4 in the edit/i });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(onUseInEdit).toHaveBeenCalledTimes(1);
    expect(onUseInEdit.mock.calls[0][0].gcs_path).toBe("users/u1/plan/item-1/pool/rec.mp4");
  });

  it("shows \u201cIn edit \u2713\u201d instead of the button once the path is attached", async () => {
    process.env[FLAG] = "true";
    const video = makeAsset({
      kind: "video",
      status: "ready",
      gcs_path: "users/u1/plan/item-1/pool/rec.mp4",
    });
    mockFetch(listRoute([video]));
    await act(async () => {
      render(
        <AssetPool
          itemId="item-1"
          attachedPaths={["users/u1/plan/item-1/pool/rec.mp4"]}
          onUseInEdit={jest.fn()}
        />,
      );
    });

    expect(screen.getByText("In edit \u2713")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /use .* in the edit/i })).toBeNull();
  });

  it("never renders the affordance on image assets or without a handler", async () => {
    process.env[FLAG] = "true";
    const image = makeAsset({ kind: "image", status: "ready" });
    mockFetch(listRoute([image]));
    await act(async () => {
      render(<AssetPool itemId="item-1" attachedPaths={[]} onUseInEdit={jest.fn()} />);
    });
    expect(screen.queryByRole("button", { name: /in the edit/i })).toBeNull();

    // No handler prop (pool-only surfaces) \u2192 no affordance on videos either.
    const video = makeAsset({ kind: "video", status: "ready" });
    mockFetch(listRoute([video]));
    await act(async () => {
      render(<AssetPool itemId="item-1" />);
    });
    expect(screen.queryByRole("button", { name: /in the edit/i })).toBeNull();
  });

  it("hides the affordance when gcs_path is missing (old-API version skew)", async () => {
    process.env[FLAG] = "true";
    // An old API's PoolAssetOut has no gcs_path — promotion must not render,
    // or clicking would send undefined -> JSON null -> 422 from attach_clips.
    const video = makeAsset({ kind: "video", status: "ready", gcs_path: "" });
    mockFetch(listRoute([video]));
    await act(async () => {
      render(<AssetPool itemId="item-1" attachedPaths={[]} onUseInEdit={jest.fn()} />);
    });
    expect(screen.queryByRole("button", { name: /in the edit/i })).toBeNull();
  });

  it("hides the affordance while the video is still analyzing", async () => {
    process.env[FLAG] = "true";
    const video = makeAsset({ kind: "video", status: "analyzing" });
    mockFetch(listRoute([video]));
    await act(async () => {
      render(<AssetPool itemId="item-1" attachedPaths={[]} onUseInEdit={jest.fn()} />);
    });
    expect(screen.queryByRole("button", { name: /in the edit/i })).toBeNull();
  });

  it("disables promotion while another attach writer is busy", async () => {
    process.env[FLAG] = "true";
    const video = makeAsset({ kind: "video", status: "ready" });
    mockFetch(listRoute([video]));
    await act(async () => {
      render(
        <AssetPool
          itemId="item-1"
          attachedPaths={[]}
          onUseInEdit={jest.fn()}
          attachBusy
        />,
      );
    });
    expect(screen.getByRole("button", { name: /in the edit/i })).toBeDisabled();
  });
});

describe("AssetPool — analysis status polling", () => {
  const LIST_URL = "/api/plan/plan-items/item-1/assets";

  afterEach(() => {
    jest.useRealTimers();
  });

  it("refetches every 5s while analyzing and flips the tile in place, then stops", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    const analyzing = makeAsset({ id: "asset-a", status: "analyzing", subject: null });
    const ready = { ...analyzing, status: "ready", subject: "checkout screen" };
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return jsonResponse({ assets: [listCalls === 1 ? analyzing : ready], max_assets: 20 });
      }
      return undefined;
    });
    await renderPool();
    expect(screen.getByText("Analyzing…")).toBeInTheDocument();

    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });
    expect(listCalls).toBe(2);
    expect(screen.getByText("checkout screen")).toBeInTheDocument();
    expect(screen.queryByText("Analyzing…")).toBeNull();

    // Every asset terminal → the interval is torn down; no further fetches.
    await act(async () => {
      await jest.advanceTimersByTimeAsync(20_000);
    });
    expect(listCalls).toBe(2);
  });

  it("keeps polling through status=uploaded (analysis not yet dispatched)", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    const uploaded = makeAsset({ id: "asset-u", status: "uploaded", subject: null });
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return jsonResponse({ assets: [uploaded], max_assets: 20 });
      }
      return undefined;
    });
    await renderPool();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(listCalls).toBe(3); // mount + 2 ticks — still non-terminal, keep going
  });

  it("never starts polling when every asset is already terminal", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return jsonResponse({
          assets: [
            makeAsset({ status: "ready", subject: "done" }),
            makeAsset({ status: "failed", display_url: null }),
          ],
          max_assets: 20,
        });
      }
      return undefined;
    });
    await renderPool();
    expect(listCalls).toBe(1);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(20_000);
    });
    expect(listCalls).toBe(1);
  });

  it("epoch guard: a poll racing a delete does not resurrect the removed tile", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    // Keeps polling alive; the second tile is the one we delete mid-poll.
    const spinner = makeAsset({ id: "asset-spin", status: "analyzing", subject: null });
    const victim = makeAsset({
      id: "asset-victim",
      status: "ready",
      subject: "doomed",
      source_filename: "victim.png",
    });
    let getCalls = 0;
    let releasePoll: (() => void) | null = null;
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url === LIST_URL) {
        getCalls += 1;
        if (getCalls === 1) {
          return jsonResponse({ assets: [spinner, victim], max_assets: 20 });
        }
        // The poll GET stays in flight until we release it — the server hasn't
        // processed the delete yet, so it still returns the victim tile.
        return await new Promise<Response>((resolve) => {
          releasePoll = () => resolve(jsonResponse({ assets: [spinner, victim], max_assets: 20 }));
        });
      }
      if (method === "DELETE" && url === `${LIST_URL}/asset-victim`) {
        return jsonResponse({ ok: true });
      }
      throw new Error(`Unmocked fetch: ${method} ${url}`);
    }) as unknown as typeof fetch;

    await renderPool();
    expect(screen.getByText("doomed")).toBeInTheDocument();

    // Fire the poll tick → its GET is now in flight (releasePoll set, unresolved).
    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });
    expect(releasePoll).not.toBeNull();

    // Delete the victim WHILE the poll is in flight → bumps the epoch.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove victim.png" }));
    });
    await waitFor(() => expect(screen.queryByText("doomed")).toBeNull());

    // Resolve the stale poll — it carries the pre-delete epoch, so the guard
    // drops it and the victim stays gone (no resurrection).
    await act(async () => {
      releasePoll!();
      await Promise.resolve();
    });
    expect(screen.queryByText("doomed")).toBeNull();
  });

  it("stops polling and shows the unavailable line on a mid-poll 404", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    const analyzing = makeAsset({ id: "asset-a", status: "analyzing", subject: null });
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return listCalls === 1
          ? jsonResponse({ assets: [analyzing], max_assets: 20 })
          : jsonResponse({ detail: "Auto-placement not available." }, 404);
      }
      return undefined;
    });
    await renderPool();
    expect(screen.getByText("Analyzing…")).toBeInTheDocument();

    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });
    expect(listCalls).toBe(2);
    expect(screen.getByText("Visuals pool isn't available right now.")).toBeInTheDocument();

    // Effect tore down on unavailable → no further polling.
    await act(async () => {
      await jest.advanceTimersByTimeAsync(20_000);
    });
    expect(listCalls).toBe(2);
  });

  it("preserves the existing signed display_url across polls (no thumbnail reload)", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    // A ready tile renders an <img src={display_url}>; a spinner keeps polling on.
    const ready = makeAsset({
      id: "asset-ready",
      status: "ready",
      subject: "dash",
      display_url: "https://storage.example/bucket/dash.png?signature=v1",
    });
    const spinner = makeAsset({ id: "asset-spin", status: "analyzing", subject: null });
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        // Every read re-signs → a NEW url each time (GCS V4 behavior).
        return jsonResponse({
          assets: [
            { ...ready, display_url: "https://storage.example/bucket/dash.png?signature=v2" },
            spinner,
          ],
          max_assets: 20,
        });
      }
      return undefined;
    });
    // First mount call must carry the original v1 url so we can assert it sticks.
    (global.fetch as jest.Mock).mockImplementationOnce(async () =>
      jsonResponse({ assets: [ready, spinner], max_assets: 20 }),
    );
    await renderPool();
    expect(screen.getByAltText("dash")).toHaveAttribute(
      "src",
      "https://storage.example/bucket/dash.png?signature=v1",
    );

    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });
    // The poll re-signed to v2, but the merge kept the still-valid v1 → the
    // <img> src never changes, so the browser never reloads the thumbnail.
    expect(screen.getByAltText("dash")).toHaveAttribute(
      "src",
      "https://storage.example/bucket/dash.png?signature=v1",
    );
  });

  it("replaces the raw HEIC URL when analysis produces a browser-safe preview", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    const analyzing = makeAsset({
      id: "asset-heic",
      kind: "image",
      status: "analyzing",
      subject: null,
      display_url:
        "https://storage.googleapis.com/nova/users/u1/plan/item-1/pool/photo.heic?X-Goog-Signature=raw",
    });
    const ready = {
      ...analyzing,
      status: "ready",
      subject: "Corfu coastline",
      display_url:
        "https://storage.googleapis.com/nova/users/u1/plan/item-1/pool/photo.heic.preview.jpg?X-Goog-Signature=preview",
    };
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return jsonResponse({ assets: [listCalls === 1 ? analyzing : ready], max_assets: 20 });
      }
      return undefined;
    });

    await renderPool();
    expect(screen.getByText("Analyzing…")).toBeInTheDocument();

    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByAltText("Corfu coastline")).toHaveAttribute(
      "src",
      "https://storage.googleapis.com/nova/users/u1/plan/item-1/pool/photo.heic.preview.jpg?X-Goog-Signature=preview",
    );
  });

  it("keeps the existing URL when a fresh signing attempt returns no URL", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    const analyzing = makeAsset({
      id: "asset-signing-gap",
      status: "analyzing",
      subject: null,
      display_url: "https://storage.example/bucket/photo.jpg?signature=working",
    });
    const ready = {
      ...analyzing,
      status: "ready",
      subject: "Signed preview",
      display_url: null,
    };
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return jsonResponse({ assets: [listCalls === 1 ? analyzing : ready], max_assets: 20 });
      }
      return undefined;
    });

    await renderPool();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByAltText("Signed preview")).toHaveAttribute(
      "src",
      "https://storage.example/bucket/photo.jpg?signature=working",
    );
  });

  it("accepts the fresh URL when a storage provider returns an invalid prior URL", async () => {
    process.env[FLAG] = "true";
    jest.useFakeTimers();
    const analyzing = makeAsset({
      id: "asset-invalid-url",
      status: "analyzing",
      subject: null,
      display_url: "not a valid url",
    });
    const ready = {
      ...analyzing,
      status: "ready",
      subject: "Recovered preview",
      display_url: "https://storage.example/bucket/recovered.jpg?signature=fresh",
    };
    let listCalls = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === LIST_URL) {
        listCalls += 1;
        return jsonResponse({ assets: [listCalls === 1 ? analyzing : ready], max_assets: 20 });
      }
      return undefined;
    });

    await renderPool();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByAltText("Recovered preview")).toHaveAttribute(
      "src",
      "https://storage.example/bucket/recovered.jpg?signature=fresh",
    );
  });
});

describe("AssetPool — brand micro-label (analysis v5)", () => {
  it("exposes detected brands via the subject line's title attribute", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      status: "ready",
      subject: "checkout screen",
      brands: ["Acme", "Duolingo"],
    });
    mockFetch(listRoute([asset]));
    await renderPool();
    expect(screen.getByText("checkout screen")).toHaveAttribute(
      "title",
      "Brands: Acme, Duolingo",
    );
  });

  it("adds no title when brands are empty or absent (legacy analyses)", async () => {
    process.env[FLAG] = "true";
    const empty = makeAsset({ id: "asset-e", status: "ready", subject: "no brands", brands: [] });
    const legacy = makeAsset({ id: "asset-l", status: "ready", subject: "old analysis" });
    delete (legacy as Record<string, unknown>).brands;
    mockFetch(listRoute([empty, legacy]));
    await renderPool();
    expect(screen.getByText("no brands")).not.toHaveAttribute("title");
    expect(screen.getByText("old analysis")).not.toHaveAttribute("title");
  });
});

describe("AssetPool — creator context", () => {
  it("does not label a ready video as pending when its optional description is empty", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-ready-video",
      kind: "video",
      status: "ready",
      subject: "sunset over lake and mountains",
      nova_description: "",
      nova_on_screen_text: "",
    });
    mockFetch(listRoute([asset]));

    await renderPool();

    expect(screen.getByText("Analysis complete")).toBeInTheDocument();
    expect(screen.queryByText("Analysis pending")).not.toBeInTheDocument();
  });

  it("uses on-screen copy when the optional long description is empty", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-on-screen-copy",
      status: "ready",
      nova_description: "   ",
      nova_on_screen_text: "Sunset over the bay",
    });
    mockFetch(listRoute([asset]));

    await renderPool();

    expect(screen.getByText("Sunset over the bay")).toBeInTheDocument();
    expect(screen.queryByText("Analysis complete")).not.toBeInTheDocument();
  });

  it("labels a filename-only fallback without overstating its analysis", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-stub",
      status: "ready",
      nova_description: null,
      nova_on_screen_text: null,
      source_type: "stub",
    });
    mockFetch(listRoute([asset]));

    await renderPool();

    expect(screen.getByText("Basic file details ready")).toBeInTheDocument();
    expect(screen.queryByText("Analysis complete")).not.toBeInTheDocument();
    expect(screen.queryByText("Analysis pending")).not.toBeInTheDocument();
  });

  it("labels user context separately from Nova analysis and saves edits", async () => {
    process.env[FLAG] = "true";
    const onMutated = jest.fn();
    const asset = makeAsset({
      id: "asset-context",
      status: "ready",
      subject: "chart",
      user_context: "",
      nova_description: "Nova sees a generic chart",
    });
    const updated = {
      ...asset,
      user_context: "Use this when I mention churn",
    };
    const onAssetContextUpdated = jest.fn();
    let patchBody: unknown = null;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === "/api/plan/plan-items/item-1/assets") {
        return jsonResponse({ assets: [asset], max_assets: 20 });
      }
      if (
        method === "PATCH" &&
        url === "/api/plan/plan-items/item-1/assets/asset-context/context"
      ) {
        patchBody = JSON.parse(String(init?.body ?? "{}"));
        return jsonResponse(updated);
      }
      return undefined;
    });
    await act(async () => {
      render(
        <AssetPool
          itemId="item-1"
          onAssetContextUpdated={onAssetContextUpdated}
          onMutated={onMutated}
        />,
      );
    });

    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByText("Kria")).toBeInTheDocument();
    expect(screen.getByText("Nova sees a generic chart")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add context" }));
    fireEvent.change(screen.getByPlaceholderText("What should Kria know about this visual?"), {
      target: { value: "Use this when I mention churn" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onAssetContextUpdated).toHaveBeenCalledWith(updated);
    });
    expect(patchBody).toEqual({ user_context: "Use this when I mention churn" });
    expect(onMutated).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Use this when I mention churn")).toBeInTheDocument();
  });
});

describe("AssetPool — preview pipeline (HEIC/HEVC uploads)", () => {
  it("falls back to the kind-label placeholder when the image thumbnail fails to decode", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-broken-heic",
      kind: "image",
      status: "ready",
      subject: "phone photo",
      display_url: "https://storage.example/signed/photo.heic",
    });
    mockFetch(listRoute([asset]));
    await renderPool();

    const img = screen.getByAltText("phone photo");
    await act(async () => {
      fireEvent.error(img);
    });

    expect(screen.queryByAltText("phone photo")).toBeNull();
    expect(screen.getByText("image")).toBeInTheDocument();
  });

  it("passes preview_url as the poster on video tiles", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-video-preview",
      kind: "video",
      status: "ready",
      subject: "clip",
      display_url: "https://storage.example/signed/clip.mov",
      preview_url: "https://storage.example/signed/clip.mov.preview.jpg",
    });
    mockFetch(listRoute([asset]));
    let container!: HTMLElement;
    await act(async () => {
      ({ container } = render(<AssetPool itemId="item-1" />));
    });

    const video = container.querySelector("video");
    expect(video).toHaveAttribute(
      "poster",
      "https://storage.example/signed/clip.mov.preview.jpg",
    );
  });

  it("renders no poster when preview_url is absent (never attempted / failed)", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-video-no-preview",
      kind: "video",
      status: "ready",
      subject: "clip",
      display_url: "https://storage.example/signed/clip.mov",
    });
    mockFetch(listRoute([asset]));
    let container!: HTMLElement;
    await act(async () => {
      ({ container } = render(<AssetPool itemId="item-1" />));
    });

    expect(container.querySelector("video")).not.toHaveAttribute("poster");
  });

  it("falls back to the kind-label placeholder when the video thumbnail fails to load", async () => {
    process.env[FLAG] = "true";
    const asset = makeAsset({
      id: "asset-video-broken",
      kind: "video",
      status: "ready",
      subject: "clip",
      display_url: "https://storage.example/signed/clip.mov",
    });
    mockFetch(listRoute([asset]));
    let container!: HTMLElement;
    await act(async () => {
      ({ container } = render(<AssetPool itemId="item-1" />));
    });

    const video = container.querySelector("video")!;
    await act(async () => {
      fireEvent.error(video);
    });

    expect(container.querySelector("video")).toBeNull();
    expect(screen.getByText("video")).toBeInTheDocument();
  });
});
