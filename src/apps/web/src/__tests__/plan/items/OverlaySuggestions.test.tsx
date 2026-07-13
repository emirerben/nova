/**
 * Tests for _editor/OverlaySuggestions.tsx — the "AI suggestions" section
 * inside the editor's Overlays drawer (+ its useEditorOverlaySuggestions hook).
 *
 * Covers:
 *   1. Rows render from a pending GET (status "ready") with hedged copy,
 *      time ranges and the sfx child line; row click seeks to start−1s.
 *   2. ✓ Accept hands the envelope to onAccept and removes the row (no
 *      whole-set dismiss while accepted envelopes must survive for the commit).
 *   3. × Dismiss removes the row and clears the emptied pending set via the
 *      dismiss endpoint (pure-reject path only).
 *   4. Empty pool → upload CTA + disabled Place-visuals with inline reason.
 *   5. Suggest POST → matching pulse → poll flips to ready (fake timers).
 *   6. zero → "No confident matches…" + wishlist; failed → Retry re-POSTs.
 *
 * fetch is mocked at the global URL level (same pattern as
 * SuggestionRail.test.tsx) so the plan-api URL contract is exercised.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import OverlaySuggestions, {
  hedgedReason,
} from "@/app/plan/items/[id]/_editor/OverlaySuggestions";
import { useEditorOverlaySuggestions } from "@/app/plan/items/[id]/_editor/useEditorOverlaySuggestions";
import { listPoolAssets, type OverlaySuggestion, type PoolAsset } from "@/lib/plan-api";

const ASSETS_URL = "/api/plan/plan-items/item-1/assets";
const SUGGESTIONS_URL = "/api/plan/plan-items/item-1/variants/var-1/overlay-suggestions";
const SUGGEST_URL = "/api/plan/plan-items/item-1/variants/var-1/suggest-overlays";
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

function makeSuggestion(overrides: Record<string, unknown> = {}): OverlaySuggestion {
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
  } as OverlaySuggestion;
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

/** fetch mock routing on (method, url) — throws on anything unmocked. */
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

async function renderSection(
  props: Partial<React.ComponentProps<typeof OverlaySuggestions>> = {},
) {
  const onAccept = jest.fn();
  const onSeek = jest.fn();
  function Harness() {
    const suggestions = useEditorOverlaySuggestions({
      itemId: "item-1",
      variantId: "var-1",
      enabled: true,
    });
    const [assets, setAssets] = React.useState<PoolAsset[]>([]);
    const [maxAssets, setMaxAssets] = React.useState(20);
    const [poolError, setPoolError] = React.useState<string | null>(null);
    React.useEffect(() => {
      let cancelled = false;
      listPoolAssets("item-1")
        .then((res) => {
          if (cancelled) return;
          setAssets(res.assets);
          setMaxAssets(res.max_assets);
        })
        .catch((err) => {
          if (!cancelled) setPoolError(err instanceof Error ? err.message : "pool error");
        });
      return () => {
        cancelled = true;
      };
    }, []);
    return (
      <OverlaySuggestions
        itemId="item-1"
        variantId="var-1"
        suggestions={suggestions}
        assets={assets}
        maxAssets={maxAssets}
        pending={[]}
        poolUnavailable={false}
        poolError={poolError}
        onFiles={() => {}}
        onRemoveAsset={() => {}}
        onAccept={onAccept}
        onSeek={onSeek}
        {...props}
      />
    );
  }
  await act(async () => {
    render(<Harness />);
  });
  return { onAccept, onSeek };
}

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe("hedgedReason", () => {
  it("hedges 'likely' rows and anchors confident rows that don't quote the anchor", () => {
    expect(
      hedgedReason(makeSuggestion({ confidence_tier: "likely", reason: "it could match." })),
    ).toBe("This might fit — it could match.");
    expect(
      hedgedReason(
        makeSuggestion({ transcript_anchor: "the pricing page", reason: "shows the pricing." }),
      ),
    ).toBe("You say “the pricing page” here — shows the pricing.");
    // Server reason already quotes the anchor — no double prefix.
    expect(hedgedReason(makeSuggestion())).toBe(
      "You say “it builds a payload” — this diagram shows it.",
    );
  });
});

describe("OverlaySuggestions — rows from a pending ready set", () => {
  function readyRoutes(suggestions: unknown[], extra: Record<string, unknown> = {}) {
    return mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse({ status: "ready", suggestions, ...extra }));
      }
      if (method === "POST" && url === DISMISS_URL) {
        return jsonResponse({ ok: true });
      }
      return undefined;
    });
  }

  it("renders rows with hedged copy, time range and the sfx child line", async () => {
    const withSfx = makeSuggestion({
      id: "sug-1",
      sfx: {
        id: "sfx-1",
        at_s: 5,
        gain: 1,
        sound_effect_id: "se-pop",
        src_gcs_path: "sound-effects/pop.mp3",
        label: "pop",
      },
    });
    const likely = makeSuggestion({
      id: "sug-2",
      confidence_tier: "likely",
      reason: "you describe your setup here.",
      overlay: { ...makeSuggestion().overlay, id: "ov-2", start_s: 38, end_s: 47 },
    });
    readyRoutes([withSfx, likely]);
    await renderSection();

    expect(
      screen.getByText("You say “it builds a payload” — this diagram shows it."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("This might fit — you describe your setup here."),
    ).toBeInTheDocument();
    expect(screen.getByText(/0:05–0:14/)).toBeInTheDocument();
    expect(screen.getByText(/0:38–0:47/)).toBeInTheDocument();
    expect(screen.getByText(/\+ pop sound/)).toBeInTheDocument();
    // With rows pending, the entry button flips to re-match.
    expect(screen.getByRole("button", { name: /re-match visuals/i })).toBeEnabled();
  });

  it("row click seeks the editor transport to max(0, start_s − 1)", async () => {
    readyRoutes([makeSuggestion({ id: "sug-1" })]);
    const { onSeek } = await renderSection();

    fireEvent.click(screen.getByRole("button", { name: /preview suggestion/i }));
    expect(onSeek).toHaveBeenCalledWith(4); // start_s 5 − 1
  });

  it("✓ Accept hands the envelope to onAccept, removes the row, never dismisses the set", async () => {
    const row = makeSuggestion({ id: "sug-1" });
    const fetchSpy = readyRoutes([row]);
    const { onAccept } = await renderSection();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept payload.png" }));
    });
    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onAccept.mock.calls[0][0].id).toBe("sug-1");
    expect(screen.queryByRole("button", { name: "Accept payload.png" })).toBeNull();
    // Accepted envelopes must survive server-side until the commit drops them.
    const dismissCalls = fetchSpy.mock.calls.filter(
      ([input, init]) =>
        String(input) === DISMISS_URL && (init?.method ?? "GET").toUpperCase() === "POST",
    );
    expect(dismissCalls).toHaveLength(0);
  });

  it("× removes the row and clears the emptied set via the dismiss endpoint", async () => {
    const fetchSpy = readyRoutes([makeSuggestion({ id: "sug-1" })]);
    await renderSection();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Dismiss payload.png" }));
    });
    expect(screen.queryByRole("button", { name: "Dismiss payload.png" })).toBeNull();
    await waitFor(() => {
      const dismissCalls = fetchSpy.mock.calls.filter(
        ([input, init]) =>
          String(input) === DISMISS_URL && (init?.method ?? "GET").toUpperCase() === "POST",
      );
      expect(dismissCalls).toHaveLength(1);
    });
  });
});

describe("OverlaySuggestions — pool gating", () => {
  it("empty pool shows the upload CTA and disables Place-visuals with an inline reason", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse());
      }
      return undefined;
    });
    await renderSection();

    expect(
      screen.getByText("Add screenshots or clips of what you talk about"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add visuals" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /place visuals for me/i })).toBeDisabled();
    expect(screen.getByText("Add at least one visual first")).toBeInTheDocument();
  });

  it("analyzing-only assets keep Place-visuals disabled (no READY asset yet)", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset({ status: "analyzing" })], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse());
      }
      return undefined;
    });
    jest.useFakeTimers();
    await renderSection();

    expect(screen.getByRole("button", { name: /place visuals for me/i })).toBeDisabled();
    expect(screen.getByText("Add at least one visual first")).toBeInTheDocument();
  });
});

describe("OverlaySuggestions — suggest → matching → ready flow", () => {
  it("POSTs suggest, shows the pulse while matching, then rows on the ready poll", async () => {
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
    await renderSection();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /place visuals for me/i }));
    });
    expect(suggestPosted).toBe(true);
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();

    // First poll: still matching.
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2500);
    });
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();

    // Second poll: the set arrives.
    suggestionsPayload = suggestionsResponse({
      status: "ready",
      suggestions: [makeSuggestion({ id: "sug-1" })],
    });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2500);
    });
    expect(screen.queryByText("Matching your visuals to the script…")).toBeNull();
    expect(
      screen.getByText("You say “it builds a payload” — this diagram shows it."),
    ).toBeInTheDocument();
  });
});

describe("OverlaySuggestions — zero and failed states", () => {
  it("zero shows the no-confident-matches copy with wishlist lines", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(
          suggestionsResponse({ status: "zero", wishlist: ["A screenshot of your dashboard"] }),
        );
      }
      return undefined;
    });
    await renderSection();

    expect(
      screen.getByText("No confident matches — try adding more specific visuals."),
    ).toBeInTheDocument();
    expect(screen.getByText("A screenshot of your dashboard")).toBeInTheDocument();
  });

  it("failed shows the error tile and Retry re-POSTs suggest", async () => {
    let suggestPosts = 0;
    mockFetch((method, url) => {
      if (method === "GET" && url === ASSETS_URL) {
        return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
      }
      if (method === "GET" && url === SUGGESTIONS_URL) {
        return jsonResponse(suggestionsResponse({ status: "failed" }));
      }
      if (method === "POST" && url === SUGGEST_URL) {
        suggestPosts += 1;
        return jsonResponse({ status: "matching" });
      }
      return undefined;
    });
    jest.useFakeTimers();
    await renderSection();

    expect(screen.getByText("Couldn't match your visuals this time.")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });
    expect(suggestPosts).toBe(1);
    expect(screen.getByText("Matching your visuals to the script…")).toBeInTheDocument();
  });
});
