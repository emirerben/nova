/**
 * Plan 009 T5 — fullscreen cutaways in the SuggestionRail.
 *
 * Covers:
 *  - the row THUMBNAIL is the mode signal: fullscreen rows render a 9:16
 *    cover-cropped portrait tile (zero rounded chrome inside, media via
 *    mediaClassFor("fullscreen")); pip rows keep the landscape rounded tile.
 *  - "Full screen" soft pill (§2 soft cell — the rail is a LIGHT surface)
 *    on the bold line BEFORE the filename.
 *  - honest reason lead: "Full-screen cutaway · {dur}s — covers you while
 *    you keep talking." then the agent's reason verbatim.
 *  - set-level summary when ≥1 fullscreen suggestion, with duration math.
 *  - one-tap "Show as small card instead" demote — routes through
 *    onSuggestionEdit with the shared demotePatch (popover parity), and
 *    falls back to internal rows state (patch + implicit stage) when the
 *    page hasn't lifted the edit path.
 *  - ARCH-4 receipt lines (demoted / dropped / both / null → gone).
 */

// @ts-nocheck

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import SuggestionRail, { receiptLines } from "@/app/plan/_components/SuggestionRail";

const FLAG = "NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED";

const ASSETS_URL = "/api/plan/plan-items/item-1/assets";
const SUGGESTIONS_URL = "/api/plan/plan-items/item-1/variants/var-1/overlay-suggestions";

function makeAsset(overrides: Record<string, unknown> = {}) {
  return {
    id: "asset-1",
    kind: "image",
    status: "ready",
    source_filename: "payload.png",
    duration_s: null,
    aspect: null,
    width: null,
    height: null,
    subject: "payload diagram",
    user_context: "",
    nova_description: "Nova sees a payload diagram",
    nova_on_screen_text: null,
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

/** A fullscreen suggestion with a 2.8s window (5 → 7.8). `overlay` override
 *  merges INTO the base overlay (never replaces it wholesale). */
function makeFullscreenSuggestion(overrides: Record<string, unknown> = {}) {
  const { overlay: overlayOverride, ...rest } = overrides;
  const base = makeSuggestion(rest);
  return {
    ...base,
    overlay: {
      ...base.overlay,
      display_mode: "fullscreen",
      start_s: 5,
      end_s: 7.8,
      ...((overlayOverride as Record<string, unknown> | undefined) ?? {}),
    },
  };
}

function suggestionsResponse(overrides: Record<string, unknown> = {}) {
  return { status: null, suggestions: [], wishlist: [], stale_cleared: false, ...overrides };
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

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

async function renderReady(
  suggestions: unknown[],
  extraProps: Record<string, unknown> = {},
) {
  process.env[FLAG] = "true";
  mockFetch((method, url) => {
    if (method === "GET" && url === ASSETS_URL) {
      return jsonResponse({ assets: [makeAsset()], max_assets: 20 });
    }
    if (method === "GET" && url === SUGGESTIONS_URL) {
      return jsonResponse(suggestionsResponse({ status: "ready", suggestions }));
    }
    return undefined;
  });
  await act(async () => {
    render(<SuggestionRail itemId="item-1" variantId="var-1" {...extraProps} />);
  });
}

afterEach(() => {
  jest.restoreAllMocks();
  delete process.env[FLAG];
});

// ── Row thumbnail = mode signal ───────────────────────────────────────────────

describe("SuggestionRail — fullscreen row tile (9:16 mini takeover)", () => {
  it("renders the fullscreen row thumbnail as a 9:16 cover-cropped tile, no rounded chrome inside", async () => {
    await renderReady([makeFullscreenSuggestion({ id: "sug-fs" })]);

    const tile = screen.getByTestId("suggestion-thumb-sug-fs");
    expect(tile).toHaveAttribute("data-thumb-mode", "fullscreen");
    expect(tile).toHaveClass("aspect-[9/16]", "w-8", "overflow-hidden");
    expect(tile.className).not.toMatch(/rounded/);

    // Media is full-bleed cover-crop via mediaClassFor("fullscreen") — zero chrome.
    const img = tile.querySelector("img")!;
    expect(img).toHaveClass("w-full", "h-full", "object-cover");
    expect(img.className).not.toMatch(/rounded/);
  });

  it("pip rows keep the landscape rounded tile unchanged", async () => {
    await renderReady([makeSuggestion({ id: "sug-pip" })]);

    const tile = screen.getByTestId("suggestion-thumb-sug-pip");
    expect(tile).toHaveAttribute("data-thumb-mode", "pip");
    expect(tile).toHaveClass("h-8", "w-11", "rounded-md");
    const img = tile.querySelector("img")!;
    expect(img).toHaveClass("h-full", "w-full", "object-cover");
  });
});

// ── Pill + honest copy ────────────────────────────────────────────────────────

describe("SuggestionRail — fullscreen pill + reason lead", () => {
  it("renders the 'Full screen' soft pill on the bold line BEFORE the filename", async () => {
    await renderReady([makeFullscreenSuggestion({ id: "sug-fs" })]);

    const pill = screen.getByText("Full screen");
    // §2 soft cell — the rail is a light/white surface, so the lime family applies.
    expect(pill).toHaveClass("border-lime-200", "bg-lime-50", "text-lime-800");
    // Pill precedes the filename on the same bold line.
    const boldLine = pill.closest("p")!;
    expect(boldLine.className).toMatch(/font-semibold/);
    expect(boldLine.textContent).toMatch(/✦\s*Full screen\s*payload\.png/);
  });

  it("leads the reason with the honest takeover line, then the agent's reason verbatim", async () => {
    await renderReady([makeFullscreenSuggestion({ id: "sug-fs" })]);

    expect(
      screen.getByText("Full-screen cutaway · 2.8s — covers you while you keep talking."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/You say “it builds a payload” — this diagram shows it\./),
    ).toBeInTheDocument();
  });

  it("pip rows show neither the pill nor the takeover lead", async () => {
    await renderReady([makeSuggestion({ id: "sug-pip" })]);

    expect(screen.queryByText("Full screen")).toBeNull();
    expect(screen.queryByText(/Full-screen cutaway/)).toBeNull();
  });
});

// ── Set-level summary ─────────────────────────────────────────────────────────

describe("SuggestionRail — fullscreen set summary", () => {
  it("2 fullscreen rows → '2 full-screen moments · 5.5s total …' (duration math)", async () => {
    await renderReady([
      makeFullscreenSuggestion({ id: "sug-a" }), // 5 → 7.8 = 2.8s
      makeFullscreenSuggestion({
        id: "sug-b",
        overlay: { start_s: 10, end_s: 12.7 }, // 2.7s
      }),
      makeSuggestion({ id: "sug-pip" }), // pip — excluded from the math
    ]);

    expect(screen.getByTestId("fullscreen-set-summary")).toHaveTextContent(
      "2 full-screen moments · 5.5s total — they cover you while you keep talking.",
    );
  });

  it("singular form for one fullscreen row", async () => {
    await renderReady([makeFullscreenSuggestion({ id: "sug-a" })]);
    expect(screen.getByTestId("fullscreen-set-summary")).toHaveTextContent(
      "1 full-screen moment · 2.8s total — they cover you while you keep talking.",
    );
  });

  it("no summary when the set has no fullscreen rows", async () => {
    await renderReady([makeSuggestion({ id: "sug-pip" })]);
    expect(screen.queryByTestId("fullscreen-set-summary")).toBeNull();
  });
});

// ── One-tap demote ────────────────────────────────────────────────────────────

describe("SuggestionRail — 'Show as small card instead' demote", () => {
  it("routes through onSuggestionEdit with the shared demotePatch (fracs kept → mode-only)", async () => {
    const onSuggestionEdit = jest.fn();
    await renderReady([makeFullscreenSuggestion({ id: "sug-fs" })], { onSuggestionEdit });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /show .* as small card instead/i }));
    });
    // The suggestion overlay carries a pip layout (x/y/scale) → demotePatch is
    // display_mode only, exactly like the popover's demote (helper reuse).
    expect(onSuggestionEdit).toHaveBeenCalledWith("sug-fs", { display_mode: "pip" });
  });

  it("born-fullscreen (no pip layout) demotes to the center preset — same rule as the popover", async () => {
    const onSuggestionEdit = jest.fn();
    await renderReady(
      [
        makeFullscreenSuggestion({
          id: "sug-fs",
          overlay: { x_frac: null, y_frac: null },
        }),
      ],
      { onSuggestionEdit },
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /show .* as small card instead/i }));
    });
    expect(onSuggestionEdit).toHaveBeenCalledWith("sug-fs", {
      display_mode: "pip",
      position: "center",
      x_frac: 0.5,
      y_frac: 0.5,
    });
  });

  it("self-contained mode (no onSuggestionEdit): demote patches the row in place + stages it", async () => {
    await renderReady([makeFullscreenSuggestion({ id: "sug-fs" })]);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /show .* as small card instead/i }));
    });

    // Row flipped to pip: pill + takeover copy gone, tile back to landscape.
    expect(screen.queryByText("Full screen")).toBeNull();
    expect(screen.queryByText(/Full-screen cutaway/)).toBeNull();
    expect(screen.getByTestId("suggestion-thumb-sug-fs")).toHaveAttribute(
      "data-thumb-mode",
      "pip",
    );
    // Edit implicitly stages (005-4A semantics mirrored): ✓ turns solid.
    expect(screen.getByRole("button", { name: /^Keep/ }).className).toMatch(/bg-lime-600/);
  });

  it("pip rows have no demote button", async () => {
    await renderReady([makeSuggestion({ id: "sug-pip" })]);
    expect(screen.queryByRole("button", { name: /as small card instead/i })).toBeNull();
  });
});

// ── ARCH-4 receipt ────────────────────────────────────────────────────────────

describe("SuggestionRail — overlay_apply_receipt render (ARCH-4, never silent)", () => {
  async function renderIdleWithReceipt(applyReceipt: unknown) {
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
      render(<SuggestionRail itemId="item-1" variantId="var-1" applyReceipt={applyReceipt} />);
    });
  }

  it("demoted with reason 'intro' → 'shown smaller to protect your intro'", async () => {
    await renderIdleWithReceipt({ demoted: 1, reason: "intro", at: "2026-07-03T00:00:00Z" });
    expect(screen.getByTestId("overlay-apply-receipt")).toHaveTextContent(
      "1 visual shown smaller to protect your intro",
    );
  });

  it("demoted with another reason → plain 'shown smaller'", async () => {
    await renderIdleWithReceipt({ demoted: 2, reason: "overlap" });
    expect(screen.getByTestId("overlay-apply-receipt")).toHaveTextContent(
      "2 visuals shown smaller",
    );
    expect(screen.queryByText(/protect your intro/)).toBeNull();
  });

  it("dropped → 'couldn't fit and were skipped'", async () => {
    await renderIdleWithReceipt({ dropped: 2, reason: "overlap" });
    expect(screen.getByTestId("overlay-apply-receipt")).toHaveTextContent(
      "2 visuals couldn't fit and were skipped",
    );
  });

  it("both demoted and dropped render both lines", async () => {
    await renderIdleWithReceipt({ demoted: 1, dropped: 1, reason: "hook" });
    const receipt = screen.getByTestId("overlay-apply-receipt");
    expect(receipt).toHaveTextContent("1 visual shown smaller to protect your intro");
    expect(receipt).toHaveTextContent("1 visual couldn't fit and was skipped");
  });

  it("null receipt → no line", async () => {
    await renderIdleWithReceipt(null);
    expect(screen.queryByTestId("overlay-apply-receipt")).toBeNull();
  });

  it("receiptLines pluralization + reason table (unit)", () => {
    expect(receiptLines(null)).toEqual([]);
    expect(receiptLines(undefined)).toEqual([]);
    expect(receiptLines({ demoted: 0, dropped: 0 })).toEqual([]);
    expect(receiptLines({ demoted: 1, reason: "hook" })).toEqual([
      "1 visual shown smaller to protect your intro",
    ]);
    expect(receiptLines({ demoted: 3, reason: "intro" })).toEqual([
      "3 visuals shown smaller to protect your intro",
    ]);
    expect(receiptLines({ demoted: 1 })).toEqual(["1 visual shown smaller"]);
    expect(receiptLines({ dropped: 1 })).toEqual(["1 visual couldn't fit and was skipped"]);
    expect(receiptLines({ demoted: 2, dropped: 3, reason: "overlap" })).toEqual([
      "2 visuals shown smaller",
      "3 visuals couldn't fit and were skipped",
    ]);
  });
});
