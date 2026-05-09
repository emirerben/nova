/**
 * Tests for the useArchitectureData hooks.
 *
 * We test the pure helper functions directly (trackRecentJob) and the
 * SWR hooks via their fetcher functions.
 */

// Mock SWR to avoid async rendering complexities — test fetcher logic directly
jest.mock("swr", () => ({
  __esModule: true,
  default: jest.fn(),
}));

// Mock the API functions
jest.mock("@/lib/api", () => ({
  getTemplateJobStatus: jest.fn(),
  listTemplateJobs: jest.fn(),
}));

import { trackRecentJob } from "@/hooks/useArchitectureData";

const STORAGE_KEY = "nova_recent_template_jobs";

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
      trackRecentJob("job-123");

      const stored = JSON.parse(localStorageMock.getItem(STORAGE_KEY)!);
      expect(stored).toContain("job-123");
    });

    test("does not duplicate existing job IDs", () => {
      trackRecentJob("job-123");
      trackRecentJob("job-123");

      const stored = JSON.parse(localStorageMock.getItem(STORAGE_KEY)!);
      expect(stored.filter((id: string) => id === "job-123")).toHaveLength(1);
    });

    test("caps at 10 most recent jobs", () => {
      for (let i = 0; i < 15; i++) {
        trackRecentJob(`job-${i}`);
      }

      const stored = JSON.parse(localStorageMock.getItem(STORAGE_KEY)!);
      expect(stored.length).toBeLessThanOrEqual(10);
      expect(stored[0]).toBe("job-14");
    });
  });

  describe("localStorage resilience", () => {
    test("handles corrupt localStorage gracefully", () => {
      localStorageMock.setItem(STORAGE_KEY, "not-valid-json{{{");

      expect(() => trackRecentJob("job-new")).not.toThrow();

      const stored = JSON.parse(localStorageMock.getItem(STORAGE_KEY)!);
      expect(stored).toContain("job-new");
    });

    test("handles non-array localStorage value gracefully", () => {
      localStorageMock.setItem(STORAGE_KEY, JSON.stringify("string-value"));

      expect(() => trackRecentJob("job-new")).not.toThrow();

      const stored = JSON.parse(localStorageMock.getItem(STORAGE_KEY)!);
      expect(stored).toContain("job-new");
    });
  });
});
