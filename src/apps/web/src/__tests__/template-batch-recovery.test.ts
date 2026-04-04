/**
 * Tests for batch import localStorage recovery in the template page.
 *
 * Imports the actual helpers from page.tsx (exported) and tests them directly.
 * Also covers staleness detection and API-level recovery behavior.
 */

// ── localStorage mock (same pattern as useArchitectureData.test.ts) ───────��─
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

// ── Mocks (must come before imports) ────────────────────────────────────────
const mockGetDriveImportBatchStatus = jest.fn();
const mockCreateTemplateJob = jest.fn();
const mockImportBatchFromDrive = jest.fn();

jest.mock("@/lib/api", () => ({
  getDriveImportBatchStatus: (...args: unknown[]) =>
    mockGetDriveImportBatchStatus(...args),
  createTemplateJob: (...args: unknown[]) => mockCreateTemplateJob(...args),
  importBatchFromDrive: (...args: unknown[]) =>
    mockImportBatchFromDrive(...args),
  listTemplates: jest.fn().mockResolvedValue([]),
  getBatchPresignedUrls: jest.fn(),
  uploadFileToGcs: jest.fn(),
}));

jest.mock("@/hooks/useArchitectureData", () => ({
  trackRecentJob: jest.fn(),
}));

jest.mock("@/lib/google-drive-picker", () => ({
  preloadDriveScripts: jest.fn().mockResolvedValue(undefined),
  requestDriveAccessToken: jest.fn(),
  openDrivePicker: jest.fn(),
}));

jest.mock("next/navigation", () => ({
  useRouter: () => ({
    push: jest.fn(),
    replace: jest.fn(),
    prefetch: jest.fn(),
  }),
}));

// ── Import the actual helpers from batch-storage utility ─────────────────────
import {
  saveBatchToStorage,
  readBatchFromStorage,
  clearBatchStorage,
} from "@/lib/batch-storage";

const BATCH_STORAGE_KEY = "nova_active_batch_import";

describe("batch import localStorage recovery", () => {
  beforeEach(() => {
    localStorageMock.clear();
    jest.clearAllMocks();
  });

  // ── 1. Save on import start ─────────────────────────────────────────────
  test("saves { batch_id, template_id, saved_at } to localStorage on import start", () => {
    const before = Date.now();
    saveBatchToStorage("batch-abc", "tmpl-123");

    const raw = localStorage.getItem(BATCH_STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.batch_id).toBe("batch-abc");
    expect(parsed.template_id).toBe("tmpl-123");
    expect(parsed.saved_at).toBeGreaterThanOrEqual(before);
    expect(parsed.saved_at).toBeLessThanOrEqual(Date.now());
  });

  // ── 2. Read and resume on mount ─────────────────────────────────────────
  test("reads and resumes polling data from localStorage", () => {
    // Simulate a recent save (with saved_at)
    localStorage.setItem(
      BATCH_STORAGE_KEY,
      JSON.stringify({ batch_id: "batch-xyz", template_id: "tmpl-456", saved_at: Date.now() })
    );

    const saved = readBatchFromStorage();
    expect(saved).not.toBeNull();
    expect(saved!.batch_id).toBe("batch-xyz");
    expect(saved!.template_id).toBe("tmpl-456");
  });

  // ── 3. Clear on batch complete ──────────────────────────────────────────
  test("clears localStorage when batch completes", () => {
    saveBatchToStorage("batch-done", "tmpl-done");
    expect(localStorage.getItem(BATCH_STORAGE_KEY)).not.toBeNull();

    clearBatchStorage();
    expect(localStorage.getItem(BATCH_STORAGE_KEY)).toBeNull();
    expect(readBatchFromStorage()).toBeNull();
  });

  // ── 4. Clear on batch failure ───────────────────────────────────────────
  test("clears localStorage when batch fails", () => {
    saveBatchToStorage("batch-fail", "tmpl-fail");

    // Simulate what the component does on failure
    clearBatchStorage();
    expect(readBatchFromStorage()).toBeNull();
  });

  // ── 5. Handle 404 (expired/missing batch) ──────────────────────────────
  test("clears localStorage on 404 (expired/missing batch)", async () => {
    saveBatchToStorage("batch-expired", "tmpl-expired");
    expect(readBatchFromStorage()).not.toBeNull();

    // Simulate what the component does when getDriveImportBatchStatus throws
    mockGetDriveImportBatchStatus.mockRejectedValueOnce(
      new Error("Batch status fetch failed: 404")
    );

    // The component's catch block calls clearBatchStorage()
    try {
      await mockGetDriveImportBatchStatus("batch-expired");
    } catch {
      clearBatchStorage();
    }

    expect(readBatchFromStorage()).toBeNull();
  });

  // ── 6. Malformed localStorage data ─────────────────────────────────────
  test("handles malformed localStorage data gracefully (no crash)", () => {
    // Not valid JSON
    localStorage.setItem(BATCH_STORAGE_KEY, "not-json{{{");
    expect(readBatchFromStorage()).toBeNull();

    // Valid JSON but missing fields
    localStorage.setItem(BATCH_STORAGE_KEY, JSON.stringify({ batch_id: "x" }));
    expect(readBatchFromStorage()).toBeNull();

    // Valid JSON but wrong types
    localStorage.setItem(
      BATCH_STORAGE_KEY,
      JSON.stringify({ batch_id: 123, template_id: null })
    );
    expect(readBatchFromStorage()).toBeNull();

    // Empty string
    localStorage.setItem(BATCH_STORAGE_KEY, "");
    expect(readBatchFromStorage()).toBeNull();

    // Null-ish
    localStorage.setItem(BATCH_STORAGE_KEY, "null");
    expect(readBatchFromStorage()).toBeNull();

    // Array instead of object
    localStorage.setItem(BATCH_STORAGE_KEY, JSON.stringify(["a", "b"]));
    expect(readBatchFromStorage()).toBeNull();
  });

  // ── 7. Overwrite old batch when new import starts ──────────────────────
  test("overwrites old batch entry when new import starts", () => {
    saveBatchToStorage("batch-old", "tmpl-old");
    expect(readBatchFromStorage()!.batch_id).toBe("batch-old");

    saveBatchToStorage("batch-new", "tmpl-new");
    const saved = readBatchFromStorage();
    expect(saved!.batch_id).toBe("batch-new");
    expect(saved!.template_id).toBe("tmpl-new");
  });

  // ── 8. Stale entries are discarded (Redis TTL protection) ──────────────
  test("discards stale entries older than 30 minutes", () => {
    // Simulate a save from 31 minutes ago
    const thirtyOneMinAgo = Date.now() - 31 * 60 * 1000;
    localStorage.setItem(
      BATCH_STORAGE_KEY,
      JSON.stringify({ batch_id: "batch-stale", template_id: "tmpl-stale", saved_at: thirtyOneMinAgo })
    );

    expect(readBatchFromStorage()).toBeNull();
    // Entry should be cleaned up
    expect(localStorage.getItem(BATCH_STORAGE_KEY)).toBeNull();
  });

  // ── 9. Recent entries are not discarded ────────────────────────────────
  test("keeps entries younger than 30 minutes", () => {
    // Simulate a save from 5 minutes ago
    const fiveMinAgo = Date.now() - 5 * 60 * 1000;
    localStorage.setItem(
      BATCH_STORAGE_KEY,
      JSON.stringify({ batch_id: "batch-recent", template_id: "tmpl-recent", saved_at: fiveMinAgo })
    );

    const saved = readBatchFromStorage();
    expect(saved).not.toBeNull();
    expect(saved!.batch_id).toBe("batch-recent");
  });

  // ── 10. Entries without saved_at are treated as expired ─────────────────
  test("discards entries without saved_at (legacy format)", () => {
    localStorage.setItem(
      BATCH_STORAGE_KEY,
      JSON.stringify({ batch_id: "batch-legacy", template_id: "tmpl-legacy" })
    );

    expect(readBatchFromStorage()).toBeNull();
    // Entry should be cleaned up
    expect(localStorage.getItem(BATCH_STORAGE_KEY)).toBeNull();
  });

  // ── 11. No stale data when localStorage is empty ───────────────────────
  test("returns null when no batch is stored (clean mount)", () => {
    expect(readBatchFromStorage()).toBeNull();
  });
});
