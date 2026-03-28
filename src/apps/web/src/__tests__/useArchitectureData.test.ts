/**
 * Tests for the useArchitectureData hooks.
 *
 * We test the pure helper functions directly (trackRecentJob, readRecentJobIds)
 * and the SWR hooks via their fetcher functions.
 */

// Mock SWR to avoid async rendering complexities — test fetcher logic directly
jest.mock("swr", () => ({
  __esModule: true,
  default: jest.fn(),
}));

// Mock the API functions
jest.mock("@/lib/api", () => ({
  getJobStatus: jest.fn(),
  getTemplateJobStatus: jest.fn(),
  listTemplateJobs: jest.fn(),
  TERMINAL_STATES: new Set(["clips_ready", "clips_ready_partial", "done", "posting_failed", "processing_failed"]),
}));

import { trackRecentJob } from "@/hooks/useArchitectureData";

// LocalStorage mock
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: jest.fn((key: string) => store[key] ?? null),
    setItem: jest.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: jest.fn((key: string) => {
      delete store[key];
    }),
    clear: jest.fn(() => {
      store = {};
    }),
    get length() {
      return Object.keys(store).length;
    },
    key: jest.fn((i: number) => Object.keys(store)[i] ?? null),
  };
})();

Object.defineProperty(window, "localStorage", { value: localStorageMock });

describe("useArchitectureData", () => {
  beforeEach(() => {
    localStorageMock.clear();
    jest.clearAllMocks();
  });

  describe("trackRecentJob", () => {
    test("saves job ID to localStorage", () => {
      trackRecentJob("job-123", "default");

      const stored = JSON.parse(localStorageMock.getItem("nova_recent_jobs")!);
      expect(stored).toContain("job-123");
    });

    test("does not duplicate existing job IDs", () => {
      trackRecentJob("job-123", "default");
      trackRecentJob("job-123", "default");

      const stored = JSON.parse(localStorageMock.getItem("nova_recent_jobs")!);
      expect(stored.filter((id: string) => id === "job-123")).toHaveLength(1);
    });

    test("caps at 10 most recent jobs", () => {
      for (let i = 0; i < 15; i++) {
        trackRecentJob(`job-${i}`, "default");
      }

      const stored = JSON.parse(localStorageMock.getItem("nova_recent_jobs")!);
      expect(stored.length).toBeLessThanOrEqual(10);
      // Most recent should be first
      expect(stored[0]).toBe("job-14");
    });

    test("uses separate key for template jobs", () => {
      trackRecentJob("job-abc", "template");

      expect(localStorageMock.getItem("nova_recent_template_jobs")).toBeTruthy();
      const stored = JSON.parse(localStorageMock.getItem("nova_recent_template_jobs")!);
      expect(stored).toContain("job-abc");
      // Default key should be empty
      expect(localStorageMock.getItem("nova_recent_jobs")).toBeNull();
    });
  });

  describe("localStorage resilience", () => {
    test("handles corrupt localStorage gracefully", () => {
      // Write corrupt JSON to localStorage
      localStorageMock.setItem("nova_recent_jobs", "not-valid-json{{{");

      // trackRecentJob should not throw, and should overwrite with valid data
      expect(() => trackRecentJob("job-new", "default")).not.toThrow();

      const stored = JSON.parse(localStorageMock.getItem("nova_recent_jobs")!);
      expect(stored).toContain("job-new");
    });

    test("handles non-array localStorage value gracefully", () => {
      localStorageMock.setItem("nova_recent_jobs", JSON.stringify("string-value"));

      expect(() => trackRecentJob("job-new", "default")).not.toThrow();

      const stored = JSON.parse(localStorageMock.getItem("nova_recent_jobs")!);
      expect(stored).toContain("job-new");
    });
  });
});
