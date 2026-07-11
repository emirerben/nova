/**
 * Tests for plan/_components/SuggestionRail.tsx (overlay auto-placement PR2, plans/005).
 *
 * Covers:
 *   1. Flag off → renders nothing (no fetch fires).
 *   2. No ready assets → entry button disabled with INLINE reason text.
 *   3. Suggest POST → matching state (Pulse copy) → poll GET (fake timers) →
 *      rows render with reasons + hedged "likely" copy verbatim.
 *   4. Per-row × removes the row; ✓ keeps; Apply sends ONLY kept suggestions.
 *   5. The sfx "×" strips sfx from that suggestion's payload, overlay stays.
 *   6. Zero state shows wishlist lines verbatim; failed state shows the Retry
 *      tile without red classes; stale_cleared shows the script-changed notice.
 *   7. aria-live polite announcement when suggestions arrive.
 *
 * fetch is mocked at the global URL level (same pattern as AssetPool.test.tsx)
 * so the plan-api URL contract is exercised.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import SuggestionRail from "@/app/plan/_components/SuggestionRail";
import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import { useOverlaySuggestionState } from "@/app/plan/_components/useOverlaySuggestions";

const FLAG = "NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED";

const ASSETS_URL = "/api/plan/plan-items/item-1/assets";
const SUGGESTIONS_URL = "/api/plan/plan-items/item-1/variants/var-1/overlay-suggestions";
const SUGGEST_URL = "/api/plan/plan-items/item-1/variants/var-1/suggest-overlays";
const APPLY_URL = "/api/plan/plan-items/item-1/variants/var-1/overlay-suggestions/apply";
const DISMISS_URL = "/api/plan/plan-items/item-1/variants/var-1/overlay-suggestions/dismiss";

function makeAsset(overrides: Record<string, unknown> = {}) {
  return {
    id: "asset-1",
    kind: "image",
    status: "ready",
    source_filename: "payload.png",
    duration_s: null,
    aspect: null,
    subject: "payload diagram",
    display_url: "https://storage.example/signed/payload.png",
    deduped: false,
    ...overrides,
  };
}

function makeSuggestion(overrides: Record<string, unknown> = {}) {
  const id = (overrides.id as string) ?? `sug-${Math.random().toString(36).slice(2)}`;
  return {
    id,
    asset_id: "asset-1",
    confidence_tier: "confident",
    reason: "You say “it builds a payload” — this diagram shows it.",
    transcript_anchor: "it builds a payload",
    overlay: {
      id: `ov-${id}`,
      kind: "image",
      src_gcs_path: "users/u1/plan/item-1/pool/payload.png",
      position: "custom",
      x_frac: 0.5,
      y_frac: 0.3,
      scale: 0.6,
      start_s: 5,
      end_s: 14,
      z: 10,
    },
    sfx: null,
    ...overrides,
  };
}

function makeSfx(overrides: Record<string, unknown> = {}) {
  return {
    id: "sfx-1",
    at_s: 5,
    gain: 1.0,
    sound_effect_id: "se-pop",
    src_gcs_path: "sound-effects/pop.mp3",
    duration_s: 0.4,
    label: "pop",
    ...overrides,
  };
}

function suggestionsResponse(overrides: Record<string, unknown> = {}) {
  return {
    status: null,
    suggestions: [],
    wishlist: [],
    stale_cleared: false,
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

/** fetch mock routing on (method, url) — returns undefined to fall through. */
function mockFetch(routes: (method: string, url: string) => Response | undefined) {
  const fn = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const res = routes(method, url);
    if (!res) throw new Error(`Unmocked fetch: ${method} ${url}`);
    return res;
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

async function renderRail() {
  await act(async () => {
    render(<SuggestionRail itemId="item-1" variantId="var-1" />);
  });
}

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
  delete process.env[FLAG];
});

describe("SuggestionRail — flag gating", () => {
  it("renders nothing when the flag is off", () => {
    // Flag deliberately unset. No fetch should fire either.
    const fetchSpy = mockFetch(() => jsonResponse(suggestionsResponse()));
    const { container } = render(<SuggestionRail itemId="item-1" variantId="var-1" />);
    expect(container).toBeEmptyDOMElement();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("SuggestionRail — variant capability gating (plan 010 OV-5)", () => {
  it("renders nothing (no fetch) when editor_capabilities.suggestions is false", () => {
    process.env[FLAG] = "true";
    const fetchSpy = mockFetch(() => jsonResponse(suggestionsResponse()));
    const { container } = render(
      <SuggestionRail itemId="item-1" variantId="var-1" suggestionsCapability={false} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("keeps the rail when the capability is true or absent (legacy payloads)", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse());
      }
      return undefined;
    });
    await act(async () => {
      render(
        <SuggestionRail itemId="item-1" variantId="var-1" suggestionsCapability={true} />,
      );
    });
    expect(
      screen.getByRole("button", { name: /place visuals for me/i }),
    ).toBeInTheDocument();
  });
});

describe("SuggestionRail — unavailable-error heuristic", () => {
  async function renderAndSuggest(detail: string) {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse());
      }
      if (method === "POST" && url === SUGGEST_URL) {
        return jsonResponse({ detail }, 400);
      }
      return undefined;
    });
    let result: ReturnType<typeof render>;
    await act(async () => {
      result = render(<SuggestionRail itemId="item-1" variantId="var-1" />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /place visuals for me/i }));
    });
    return result!;
  }

  it("maps the backend's caption-archetype 400 ('isn't available') to the calm unavailable state, not failed+Retry", async () => {
    const { container } = await renderAndSuggest(
      "Auto-placement isn't available on this edit format.",
    );
    // Calm unavailable — the rail hides entirely instead of dead-ending.
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Couldn't match your visuals this time.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("still maps the older 'not available' wording to unavailable", async () => {
    const { container } = await renderAndSuggest(
      "Auto-placement is not available for this variant.",
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("keeps genuine failures on the Retry tile (heuristic doesn't overmatch)", async () => {
    await renderAndSuggest("The matcher exploded.");
    expect(screen.getByText("Couldn't match your visuals this time.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("SuggestionRail — entry button gating", () => {
  it("disables the button with an inline reason when no ready assets", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({
          assets: [makeAsset({ status: "analyzing" })],
          max_assets: 20,
        });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse());
      }
      return undefined;
    });
    await renderRail();

    const button = screen.getByRole("button", { name: /place visuals for me/i });
    expect(button).toBeDisabled();
    // Inline reason TEXT, never tooltip-only.
    expect(screen.getByText("Add at least one visual first")).toBeInTheDocument();
  });

  it("enables the button when a ready asset exists", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse());
      }
      return undefined;
    });
    await renderRail();

    expect(screen.getByRole("button", { name: /place visuals for me/i })).toBeEnabled();
    expect(screen.queryByText("Add at least one visual first")).toBeNull();
  });
});

describe("SuggestionRail — suggest → matching → ready flow", () => {
  /** Drives the full flow: click suggest, matching pulse, poll flips to ready. */
  async function flowToReady(suggestions: unknown[]) {
    process.env[FLAG] = "true";
    let suggestionsPayload: Record<string, unknown> = suggestionsResponse();
    let suggestPosted = false;
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsPayload);
      }
      if (method === "POST" && url === SUGGEST_URL) {
        suggestPosted = true;
        suggestionsPayload = suggestionsResponse({ status: "matching" });
        return jsonResponse({ status: "matching" });
      }
      return undefined;
    });

    jest.useFakeTimers();
    await renderRail();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /place visuals for me/i }));
    });
    expect(suggestPosted).toBe(true);

    // §7 Pulse copy while the matcher runs.
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();

    // First poll still matching; second poll returns the suggestion set.
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2500);
    });
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();

    suggestionsPayload = suggestionsResponse({ status: "ready", suggestions });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2500);
    });
  }

  it("shows the pulse, polls, then renders rows with reasons + hedged copy", async () => {
    const confident = makeSuggestion({ id: "sug-1" });
    const likely = makeSuggestion({
      id: "sug-2",
      confidence_tier: "likely",
      reason: "Might match — you describe your setup file here.",
      overlay: { ...makeSuggestion().overlay, id: "ov-2", start_s: 38, end_s: 47 },
    });
    await flowToReady([confident, likely]);

    expect(screen.queryByText("Matching your visuals to the script…")).toBeNull();
    expect(screen.getByText("Suggested edit")).toBeInTheDocument();
    expect(screen.getByText("2 visuals, matched to your script")).toBeInTheDocument();
    // Reason lines verbatim — the "likely" tier keeps its hedged server copy as-is.
    expect(
      screen.getByText("You say “it builds a payload” — this diagram shows it."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Might match — you describe your setup file here."),
    ).toBeInTheDocument();
    // Time range in m:ss.
    expect(screen.getByText(/0:05–0:14/)).toBeInTheDocument();
    expect(screen.getByText(/0:38–0:47/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply 2 to video" })).toBeEnabled();
  });

  it("announces arrival via the polite live region", async () => {
    await flowToReady([makeSuggestion({ id: "sug-1" }), makeSuggestion({ id: "sug-2" })]);
    const live = screen.getByRole("status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("2 suggestions ready");
  });

  it("shows 'Still working…' after 60s of matching and keeps polling", async () => {
    process.env[FLAG] = "true";
    let suggestionsPayload: Record<string, unknown> = suggestionsResponse();
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsPayload);
      }
      if (method === "POST" && url === SUGGEST_URL) {
        suggestionsPayload = suggestionsResponse({ status: "matching" });
        return jsonResponse({ status: "matching" });
      }
      return undefined;
    });

    jest.useFakeTimers();
    await renderRail();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /place visuals for me/i }));
    });

    await act(async () => {
      await jest.advanceTimersByTimeAsync(61_000);
    });
    expect(screen.getByText("Still working…")).toBeInTheDocument();
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();
  });

  it("keeps polling past the old 5-min give-up window (R4/C12 — no fabricated failure)", async () => {
    process.env[FLAG] = "true";
    let suggestionsPayload: Record<string, unknown> = suggestionsResponse();
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsPayload);
      }
      if (method === "POST" && url === SUGGEST_URL) {
        suggestionsPayload = suggestionsResponse({ status: "matching" });
        return jsonResponse({ status: "matching" });
      }
      return undefined;
    });

    jest.useFakeTimers();
    await renderRail();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /place visuals for me/i }));
    });

    // Well past the removed 5-min GIVE_UP window (was 300s → local "failed").
    await act(async () => {
      await jest.advanceTimersByTimeAsync(400_000);
    });
    // Still matching — the client NEVER flipped itself to a failed tile.
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();
    expect(screen.queryByText("Couldn't match your visuals this time.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();

    // The poll interval is still live: a late server "ready" is picked up.
    const fetchMock = global.fetch as jest.Mock;
    const pollsBefore = fetchMock.mock.calls.filter(
      ([u]) => String(u) === SUGGESTIONS_URL,
    ).length;
    suggestionsPayload = suggestionsResponse({
      status: "ready",
      suggestions: [makeSuggestion({ id: "late-1" })],
    });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2500);
    });
    const pollsAfter = fetchMock.mock.calls.filter(
      ([u]) => String(u) === SUGGESTIONS_URL,
    ).length;
    expect(pollsAfter).toBeGreaterThan(pollsBefore); // poll never torn down
    expect(screen.getByText("1 visual, matched to your script")).toBeInTheDocument();
  });

  it("renders failed ONLY when the server returns status 'failed' (not a timeout)", async () => {
    process.env[FLAG] = "true";
    let suggestionsPayload: Record<string, unknown> = suggestionsResponse();
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsPayload);
      }
      if (method === "POST" && url === SUGGEST_URL) {
        suggestionsPayload = suggestionsResponse({ status: "matching" });
        return jsonResponse({ status: "matching" });
      }
      return undefined;
    });

    jest.useFakeTimers();
    await renderRail();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /place visuals for me/i }));
    });
    // Long wait, still matching (no client give-up).
    await act(async () => {
      await jest.advanceTimersByTimeAsync(400_000);
    });
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();

    // Now the SERVER reports failure — only then does the failed tile show.
    suggestionsPayload = suggestionsResponse({ status: "failed" });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2500);
    });
    expect(screen.getByText("Couldn't match your visuals this time.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("SuggestionRail — review interactions", () => {
  /** Renders the rail directly in ready state (initial GET returns ready). */
  async function renderReady(suggestions: unknown[]) {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse({ status: "ready", suggestions }));
      }
      if (method === "POST" && url === APPLY_URL) {
        return jsonResponse({ id: "item-1" });
      }
      if (method === "POST" && url === DISMISS_URL) {
        return jsonResponse({ ok: true });
      }
      return undefined;
    });
    await renderRail();
  }

  function lastApplyBody(): { suggestions: Array<Record<string, unknown>> } {
    const fetchMock = global.fetch as jest.Mock;
    const call = fetchMock.mock.calls.find(
      ([u, init]) => String(u) === APPLY_URL && init?.method === "POST",
    );
    expect(call).toBeTruthy();
    return JSON.parse(call![1].body as string);
  }

  it("× removes a row, ✓ keeps, and Apply sends ONLY the kept suggestions", async () => {
    const s1 = makeSuggestion({ id: "sug-1" });
    const s2 = makeSuggestion({ id: "sug-2" });
    const s3 = makeSuggestion({ id: "sug-3" });
    await renderReady([s1, s2, s3]);

    expect(screen.getByText("3 visuals, matched to your script")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply 3 to video" })).toBeInTheDocument();

    // Reject the first row — it disappears and the CTA count drops.
    const rejectButtons = screen.getAllByRole("button", { name: /^Reject/ });
    await act(async () => {
      fireEvent.click(rejectButtons[0]);
    });
    expect(screen.getAllByRole("button", { name: /^Reject/ })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Apply 2 to video" })).toBeInTheDocument();

    // ✓ keeps the (now first) remaining row — still present, stages solid.
    const keepButtons = screen.getAllByRole("button", { name: /^Keep/ });
    await act(async () => {
      fireEvent.click(keepButtons[0]);
    });
    expect(screen.getAllByRole("button", { name: /^Keep/ })).toHaveLength(2);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Apply 2 to video" }));
    });

    const body = lastApplyBody();
    expect(body.suggestions.map((s) => s.id)).toEqual(["sug-2", "sug-3"]);

    // Receipt line + rail cleared.
    expect(
      screen.getByText(/Baking your 2 visuals in — the preview above is exactly what renders\./),
    ).toBeInTheDocument();
    expect(screen.queryByText("Suggested edit")).toBeNull();
  });

  it("row is a keyboard-operable button that REVEALS (previews) on Enter/Space (R4/C11)", async () => {
    // Mount a stand-in page preview video the rail seeks via its DOM query.
    const host = document.createElement("div");
    host.setAttribute("data-variant-preview", "var-1");
    const pageVideo = document.createElement("video");
    host.appendChild(pageVideo);
    document.body.appendChild(host);
    let seeked: number | null = null;
    Object.defineProperty(pageVideo, "currentTime", {
      configurable: true,
      get: () => seeked ?? 0,
      set: (v: number) => { seeked = v; },
    });

    try {
      // start_s 5 → reveal seeks to start−1s = 4.
      await renderReady([makeSuggestion({ id: "sug-r", overlay: { ...makeSuggestion().overlay, start_s: 5, end_s: 14 } })]);

      // The row exposes itself as an interactive button to screen readers and
      // its own Enter/Space fire reveal — NOT keep (keep lives on the ✓ button).
      const row = screen.getByRole("button", { name: /^Preview suggestion:/ });
      expect(row).toHaveAttribute("tabindex", "0");

      fireEvent.keyDown(row, { key: "Enter" });
      expect(seeked).toBe(4);

      // Enter on the row did not stage the row (keep stays on the ✓ button).
      expect(screen.getByRole("button", { name: /^Keep/ }).className).not.toMatch(/bg-lime-600/);

      // Space also reveals.
      seeked = null;
      fireEvent.keyDown(row, { key: " " });
      expect(seeked).toBe(4);
    } finally {
      document.body.removeChild(host);
    }
  });

  it("the sfx × strips ONLY the sfx from that suggestion; the overlay stays", async () => {
    const withSfx = makeSuggestion({ id: "sug-sfx", sfx: makeSfx() });
    const plain = makeSuggestion({ id: "sug-plain" });
    await renderReady([withSfx, plain]);

    expect(screen.getByText(/\+ pop sound/)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /remove sound from/i }));
    });
    // The sound child line is gone but the row itself remains.
    expect(screen.queryByText(/\+ pop sound/)).toBeNull();
    expect(screen.getByRole("button", { name: "Apply 2 to video" })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Apply 2 to video" }));
    });

    const body = lastApplyBody();
    const sent = body.suggestions.find((s) => s.id === "sug-sfx")!;
    expect(sent.sfx).toBeNull();
    // Overlay payload untouched.
    expect((sent.overlay as Record<string, unknown>).src_gcs_path).toBe(
      "users/u1/plan/item-1/pool/payload.png",
    );
    expect(body.suggestions).toHaveLength(2);
  });

  it("Dismiss calls the dismiss endpoint and clears the rail", async () => {
    await renderReady([makeSuggestion({ id: "sug-1" })]);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    });
    expect(screen.queryByText("Suggested edit")).toBeNull();
    const fetchMock = global.fetch as jest.Mock;
    expect(
      fetchMock.mock.calls.some(
        ([u, init]) => String(u) === DISMISS_URL && init?.method === "POST",
      ),
    ).toBe(true);
  });
});

describe("SuggestionRail — zero / failed / stale states", () => {
  it("zero state shows the wishlist lines verbatim", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(
          suggestionsResponse({
            status: "zero",
            wishlist: [
              "Add a screenshot of the settings toggle you mention at 0:32",
              "Add a screenshot of the API console you mention at 0:41",
            ],
          }),
        );
      }
      return undefined;
    });
    await renderRail();

    expect(screen.getByText("No matching visuals yet")).toBeInTheDocument();
    expect(
      screen.getByText("Add a screenshot of the settings toggle you mention at 0:32"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Add a screenshot of the API console you mention at 0:41"),
    ).toBeInTheDocument();
  });

  it("failed state shows the dashed retry tile with no red classes", async () => {
    process.env[FLAG] = "true";
    let retried = false;
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse({ status: "failed" }));
      }
      if (method === "POST" && url === SUGGEST_URL) {
        retried = true;
        return jsonResponse({ status: "matching" });
      }
      return undefined;
    });
    const { container } = await (async () => {
      let result: ReturnType<typeof render>;
      await act(async () => {
        result = render(<SuggestionRail itemId="item-1" variantId="var-1" />);
      });
      return result!;
    })();

    expect(screen.getByText("Couldn't match your visuals this time.")).toBeInTheDocument();
    // Quiet zinc failure tone (D10), never red.
    expect(container.innerHTML).not.toMatch(/red-\d|text-red|bg-red|border-red/);

    // Single Retry action re-POSTs suggest.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });
    expect(retried).toBe(true);
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();
  });

  it("stale_cleared=true shows the script-changed notice", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse({ stale_cleared: true }));
      }
      return undefined;
    });
    await renderRail();

    expect(
      screen.getByText("Your script changed — suggestions were cleared. Place visuals again?"),
    ).toBeInTheDocument();
  });
});

// ── 006 T3: lane rendering + edit-implies-stage + Apply carries lane edits ────

/**
 * Mirrors the item page's real wiring: ONE useOverlaySuggestionState instance
 * feeds SuggestionRail (controlled rows/keptIds) AND the UnifiedTimeline lanes
 * (laneEntries + onSuggestionEdit) — exactly how page.tsx composes them.
 */
function LaneHarness() {
  const s = useOverlaySuggestionState();
  return (
    <>
      <SuggestionRail
        itemId="item-1"
        variantId="var-1"
        rows={s.rows}
        onRowsChange={s.setRows}
        keptIds={s.keptIds}
        onKeptIdsChange={s.setKeptIds}
      />
      <UnifiedTimeline
        totalDurationS={30}
        currentTimeS={0}
        sfxPlacements={[]}
        sfxGlossaryEffects={[]}
        sfxGlossaryLoading={false}
        sfxRendering={false}
        sfxUploading={false}
        onSfxChange={jest.fn()}
        onSfxUploadRequest={jest.fn().mockResolvedValue(undefined)}
        overlayCards={[]}
        overlaysEnabled={false}
        overlayUploading={false}
        localPreviewUrls={{}}
        onOverlayUploadRequest={jest.fn()}
        onUpdateCard={jest.fn()}
        onRemoveCard={jest.fn()}
        onClearOverlays={jest.fn()}
        overlaySuggestions={s.laneEntries}
        onSuggestionEdit={s.onSuggestionEdit}
      />
    </>
  );
}

describe("SuggestionRail — lane edits stage the row + ride the Apply (006 T3)", () => {
  function makeVideoSuggestion() {
    return makeSuggestion({
      id: "sug-1",
      overlay: {
        ...(makeSuggestion().overlay as Record<string, unknown>),
        id: "ov-sug-1",
        kind: "video",
        start_s: 5,
        end_s: 9,
        clip_trim_start_s: 0,
        clip_trim_end_s: 4,
        clip_duration_s: 6,
      },
    });
  }

  async function renderHarnessReady() {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(
          suggestionsResponse({ status: "ready", suggestions: [makeVideoSuggestion()] }),
        );
      }
      if (method === "POST" && url === APPLY_URL) {
        return jsonResponse({ id: "item-1" });
      }
      return undefined;
    });
    await act(async () => {
      render(<LaneHarness />);
    });
  }

  function postCalls(): Array<[string, RequestInit]> {
    const fetchMock = global.fetch as jest.Mock;
    return fetchMock.mock.calls
      .filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")
      .map(([u, init]) => [String(u), init as RequestInit]);
  }

  it("lane scale edit marks an un-✓'d row kept in the rail — with NO network call", async () => {
    await renderHarnessReady();

    // The suggestion renders in the lane with pending provenance…
    const laneCard = screen.getByRole("button", { name: /suggested overlay/i });
    expect(laneCard.className).toMatch(/border-dashed/);
    // …and the rail row is not yet kept (✓ button unconfirmed).
    const keepBtn = screen.getByRole("button", { name: /^Keep/ });
    expect(keepBtn.className).not.toMatch(/bg-lime-600/);

    // Edit in the lane: open the card popover, drag the scale slider.
    await act(async () => { fireEvent.click(screen.getByText(/▶ ov-sug/)); });
    await act(async () => {
      fireEvent.change(screen.getByRole("slider"), { target: { value: "80" } });
    });

    // Edit ⇒ implicit stage: rail ✓ flips solid, lane card dashed→solid + ✦ fade.
    expect(screen.getByRole("button", { name: /^Keep/ }).className).toMatch(/bg-lime-600/);
    const stagedCard = screen.getByRole("button", { name: /suggested overlay/i });
    expect(stagedCard.className).toMatch(/border-solid/);
    expect(stagedCard.className).not.toMatch(/border-dashed/);

    // Stage-fires-no-network: nothing POSTed yet (Apply is the only writer).
    expect(postCalls()).toHaveLength(0);
  });

  it("Apply POST body carries the lane-edited scale + trim fields", async () => {
    await renderHarnessReady();

    // Scale edit through the lane popover slider.
    await act(async () => { fireEvent.click(screen.getByText(/▶ ov-sug/)); });
    await act(async () => {
      fireEvent.change(screen.getByRole("slider"), { target: { value: "80" } });
    });

    // Clip-trim edit through the lane TrimLane handle (needs a real width).
    const rectSpy = jest
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockReturnValue({
        width: 100, height: 10, top: 0, left: 0, bottom: 10, right: 100, x: 0, y: 0,
        toJSON: () => ({}),
      } as DOMRect);
    const handle = document.querySelector('[data-trim-handle="left-ov-sug-1"]')!;
    // Separate act ticks so the drag effect's window listeners attach between
    // mousedown and mousemove (same as real event timing).
    await act(async () => { fireEvent.mouseDown(handle, { clientX: 0 }); });
    // 50px over a 100px strip on a 6s clip → trim-in +3s.
    await act(async () => { fireEvent.mouseMove(window, { clientX: 50 }); });
    await act(async () => { fireEvent.mouseUp(window); });
    rectSpy.mockRestore();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Apply 1 to video" }));
    });

    const apply = postCalls().find(([u]) => u === APPLY_URL);
    expect(apply).toBeTruthy();
    const body = JSON.parse(apply![1].body as string) as {
      suggestions: Array<{ id: string; overlay: Record<string, unknown> }>;
    };
    expect(body.suggestions).toHaveLength(1);
    expect(body.suggestions[0].id).toBe("sug-1");
    // Lane edits rode along: scale from the slider, trim from the TrimLane drag.
    expect(body.suggestions[0].overlay.scale).toBe(0.8);
    expect(body.suggestions[0].overlay.clip_trim_start_s).toBe(3);
    expect(body.suggestions[0].overlay.clip_trim_end_s).toBe(4);
  });

  it("rejecting the row in the rail removes its lane card", async () => {
    await renderHarnessReady();
    expect(screen.getByRole("button", { name: /suggested overlay/i })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Reject/ }));
    });
    expect(screen.queryByRole("button", { name: /suggested overlay/i })).toBeNull();
  });
});

describe("SuggestionRail — read-only mini-preview poster seek (006 T3)", () => {
  it("video suggestion cards seek the mini-preview poster to clip_trim_start_s", async () => {
    process.env[FLAG] = "true";
    const videoAsset = makeAsset({
      id: "asset-v",
      kind: "video",
      display_url: "https://storage.example/signed/clip.mp4",
    });
    const sug = makeSuggestion({
      id: "sug-v",
      asset_id: "asset-v",
      overlay: {
        ...(makeSuggestion().overlay as Record<string, unknown>),
        id: "ov-v",
        kind: "video",
        start_s: 5,
        end_s: 9,
        clip_trim_start_s: 3.5,
        clip_trim_end_s: 7.5,
        clip_duration_s: 12,
      },
    });
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [videoAsset], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse({ status: "ready", suggestions: [sug] }));
      }
      return undefined;
    });

    await act(async () => {
      render(
        <SuggestionRail
          itemId="item-1"
          variantId="var-1"
          previewUrl="https://storage.example/variant.mp4"
        />,
      );
    });

    const cardVideo = screen.getByTestId("mini-preview-video-sug-v") as HTMLVideoElement;

    // jsdom's HTMLMediaElement.currentTime is inert — instrument the instance.
    let seeked: number | null = null;
    Object.defineProperty(cardVideo, "currentTime", {
      configurable: true,
      get: () => seeked ?? 0,
      set: (v: number) => { seeked = v; },
    });
    await act(async () => {
      fireEvent(cardVideo, new Event("loadedmetadata"));
    });
    expect(seeked).toBe(3.5);
  });
});

describe("SuggestionRail — re-match label", () => {
  it("relabels the entry button to Re-match visuals when suggestions exist", async () => {
    process.env[FLAG] = "true";
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(
          suggestionsResponse({ status: "ready", suggestions: [makeSuggestion()] }),
        );
      }
      return undefined;
    });
    await renderRail();

    expect(screen.getByRole("button", { name: /re-match visuals/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /place visuals for me/i })).toBeNull();
  });
});
