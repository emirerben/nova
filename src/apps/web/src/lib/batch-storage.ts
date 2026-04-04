// ── Batch import localStorage persistence (survives navigation) ─────────────
const BATCH_STORAGE_KEY = "nova_active_batch_import";

const BATCH_MAX_AGE_MS = 30 * 60 * 1000; // 30 minutes — well within Redis 1h TTL

export function saveBatchToStorage(batchId: string, templateId: string) {
  if (typeof window === "undefined") return;
  localStorage.setItem(BATCH_STORAGE_KEY, JSON.stringify({ batch_id: batchId, template_id: templateId, saved_at: Date.now() }));
}

export function readBatchFromStorage(): { batch_id: string; template_id: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(BATCH_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (typeof data?.batch_id !== "string" || typeof data?.template_id !== "string") return null;
    // Discard stale entries — Redis batch keys expire in 1h
    if (typeof data.saved_at === "number" && Date.now() - data.saved_at > BATCH_MAX_AGE_MS) {
      localStorage.removeItem(BATCH_STORAGE_KEY);
      return null;
    }
    return data;
  } catch {
    return null;
  }
}

export function clearBatchStorage() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(BATCH_STORAGE_KEY);
}
