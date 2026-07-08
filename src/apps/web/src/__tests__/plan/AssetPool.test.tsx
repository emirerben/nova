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
    status: "uploaded",
    source_filename: "shot.png",
    duration_s: null,
    aspect: null,
    subject: null,
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

async function renderPool() {
  await act(async () => {
    render(<AssetPool itemId="item-1" />);
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
});

describe("AssetPool — upload flow (presigned direct-PUT, R1/C9+C14)", () => {
  const UPLOAD_URLS_URL = "/api/plan/plan-items/item-1/assets/upload-urls";
  const REGISTER_URL = "/api/plan/plan-items/item-1/assets";
  const SIGNED_PUT = "https://storage.googleapis.com/bucket/users/u1/plan/item-1/pool/shot.png";

  it("upload-urls → direct GCS PUT → register; tile appears; NO proxy body cap", async () => {
    process.env[FLAG] = "true";
    const registered = makeAsset({ subject: "settings toggle" });
    let putBody: unknown = null;
    let registerBody: Record<string, unknown> | null = null;
    mockFetch((method, url, init) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        return jsonResponse({
          urls: [{ upload_url: SIGNED_PUT, gcs_path: "users/u1/plan/item-1/pool/shot.png" }],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        putBody = init?.body;
        return jsonResponse({}, 200);
      }
      if (method === "POST" && url === REGISTER_URL) {
        registerBody = JSON.parse((init?.body as string) ?? "{}");
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
      expect(screen.getByText("settings toggle")).toBeInTheDocument();
    });

    // The bytes went straight to GCS via a direct PUT — never buffered through
    // the Next api-proxy multipart upload (that's the Vercel 4.5MB cap path).
    const fetchMock = global.fetch as jest.Mock;
    expect(fetchMock.mock.calls.some(([u]) => String(u) === SIGNED_PUT)).toBe(true);
    expect(putBody).toBe(file);
    // The legacy one-shot multipart proxy is NOT used.
    expect(
      fetchMock.mock.calls.some(
        ([u]) => String(u) === "/api/plan/plan-items/item-1/assets/upload",
      ),
    ).toBe(false);
    // Register carries the gcs_path + a client-computed content_hash for dedupe.
    expect(registerBody!.gcs_path).toBe("users/u1/plan/item-1/pool/shot.png");
    expect(registerBody!.content_type).toBe("image/png");
    expect(registerBody!.source_filename).toBe("shot.png");
    expect(typeof registerBody!.content_hash).toBe("string");
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
        return jsonResponse({
          urls: [{ upload_url: SIGNED_PUT, gcs_path: "users/u1/plan/item-1/pool/shot.png" }],
        });
      }
      if (method === "PUT" && url === SIGNED_PUT) {
        // Simulate the bucket-CORS failure: fetch throws a TypeError.
        throw new TypeError("Failed to fetch");
      }
      if (method === "POST" && url === "/api/plan/uploads/relay") {
        relayed = true;
        expect(init?.body).toBeInstanceOf(FormData);
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

  it("deduped=true → no duplicate tile + quiet notice", async () => {
    process.env[FLAG] = "true";
    const existing = makeAsset({ id: "asset-existing", subject: "dashboard" });
    mockFetch((method, url) => {
      if (method === "GET" && url === REGISTER_URL) {
        return jsonResponse({ assets: [existing], max_assets: 20 });
      }
      if (method === "POST" && url === UPLOAD_URLS_URL) {
        return jsonResponse({
          urls: [{ upload_url: SIGNED_PUT, gcs_path: "users/u1/plan/item-1/pool/dup.png" }],
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
    const addButton = screen.getByRole("button", { name: /add/i });
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
    const asset = makeAsset({ id: "asset-del", source_filename: "gone.png", subject: "toggle" });
    let deleteCalled = false;
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/plan/plan-items/item-1/assets") {
        return jsonResponse({ assets: [asset], max_assets: 20 });
      }
      if (method === "DELETE" && url === "/api/plan/plan-items/item-1/assets/asset-del") {
        deleteCalled = true;
        return jsonResponse({ ok: true });
      }
      return undefined;
    });
    await renderPool();
    expect(screen.getByText("toggle")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove gone.png" }));
    });

    expect(deleteCalled).toBe(true);
    await waitFor(() => {
      expect(screen.queryByText("toggle")).toBeNull();
    });
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
