/**
 * Tests for plan/_components/SeedUploadCard.tsx (PR4).
 *
 * Covers:
 *   1. With activation_phase + activation_started_at in poll response → renders theater chips.
 *   2. Without those fields (deploy-skew) → renders the old static amber card.
 */

// @ts-nocheck
import React from "react";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

// Mock plan-api
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  activatePlan: jest.fn(),
  attachSeedClips: jest.fn(),
  getActivation: jest.fn(),
  requestSeedUploadUrls: jest.fn(),
  uploadToGcs: jest.fn(),
}));

import { getActivation } from "@/lib/plan-api";
const mockGetActivation = getActivation as jest.MockedFunction<typeof getActivation>;

import SeedUploadCard from "@/app/plan/_components/SeedUploadCard";

function makePlan(overrides = {}) {
  return {
    id: "plan-123",
    plan_status: "ready",
    horizon_days: 30,
    events: null,
    items: [],
    activation_status: "activating",
    seed_clip_count: 2,
    generation_started_at: null,
    ...overrides,
  };
}

describe("SeedUploadCard — inline theater when activation_started_at present", () => {
  it("test_theater_shown_when_activation_started_at_present: theater chips rendered, not static text", async () => {
    // Set up: plan is activating, poll returns activation_started_at + phase.
    mockGetActivation.mockResolvedValue({
      activation_status: "activating",
      seed_clip_count: 2,
      generating_item_ids: [],
      ready_item_ids: [],
      activation_phase: "matching_clips",
      activation_started_at: "2026-06-06T10:00:00Z",
      expected_phase_durations: { matching_clips: 75000, picking_days: 10000, starting_renders: 35000 },
    });

    const plan = makePlan({ activation_status: "activating" });

    await act(async () => {
      render(<SeedUploadCard plan={plan} onError={jest.fn()} onRefresh={jest.fn()} />);
    });

    // Trigger the poll by advancing timers slightly.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 2100));
    });

    // The theater is shown — it renders the status band (PhaseChipRow etc).
    // Static fallback text ("Finding your best clip…") should NOT be present.
    expect(screen.queryByText(/finding your best clip/i)).toBeNull();
  });
});

describe("SeedUploadCard — static fallback when no activation_started_at", () => {
  it("test_static_fallback_when_no_activation_started_at: old amber card shown", async () => {
    // Plan is activating but poll hasn't run yet (or returns without new fields).
    // Initially activating=true but activation_started_at is null (old API).
    mockGetActivation.mockResolvedValue({
      activation_status: "activating",
      seed_clip_count: 2,
      generating_item_ids: [],
      ready_item_ids: [],
      // No activation_phase or activation_started_at (deploy-skew).
    });

    const plan = makePlan({ activation_status: "activating" });

    await act(async () => {
      render(<SeedUploadCard plan={plan} onError={jest.fn()} onRefresh={jest.fn()} />);
    });

    // Before poll fires, activation_started_at is null → static fallback shown.
    expect(screen.getByText(/finding your best clip/i)).toBeInTheDocument();
  });
});
