/** Client-side guard for the Save → render handoff. The renderer remains the
 * authority, but a ready response with an inconsistent receipt must not be
 * presented as a successful committed edit. */
export interface RenderVerificationInput {
  expectedDurationS: number;
  expectedGeneration?: string | null;
  expectedRevisionHash?: string | null;
  variant: {
    duration_s?: number | null;
    output_url?: string | null;
    render_generation_id?: string | null;
    render_receipt?: Record<string, unknown> | null;
  };
}

export interface RenderVerificationResult {
  ok: boolean;
  detail?: string;
  actualDurationS?: number;
}

const DURATION_TOLERANCE_S = 1 / 30 + 0.001;

export function verifyCommittedRenderDuration({
  expectedDurationS,
  expectedGeneration,
  expectedRevisionHash,
  variant,
}: RenderVerificationInput): RenderVerificationResult {
  const receipt = variant.render_receipt;
  if (!variant.output_url) {
    return { ok: false, detail: "The saved video is ready without an output URL." };
  }
  if (expectedGeneration && variant.render_generation_id !== expectedGeneration) {
    return { ok: false, detail: "The ready video belongs to a different render generation." };
  }
  if (!receipt || receipt.verified !== true) {
    return { ok: false, detail: "The saved video is ready without a verified render receipt." };
  }
  if (expectedRevisionHash && receipt.revision_hash !== expectedRevisionHash) {
    return { ok: false, detail: "The ready video receipt belongs to a different committed timeline revision." };
  }
  const actual = typeof receipt.actual_duration_s === "number" ? receipt.actual_duration_s : null;
  if (actual == null || !Number.isFinite(actual)) {
    return { ok: false, detail: "The saved video is ready without a render duration receipt." };
  }
  const receiptExpected = receipt?.expected_duration_s;
  if (typeof receiptExpected === "number" && Math.abs(receiptExpected - expectedDurationS) > DURATION_TOLERANCE_S) {
    return {
      ok: false,
      detail: `The render receipt expected ${expectedDurationS.toFixed(3)}s but recorded ${receiptExpected.toFixed(3)}s.`,
      actualDurationS: actual,
    };
  }
  if (Math.abs(actual - expectedDurationS) > DURATION_TOLERANCE_S) {
    return {
      ok: false,
      detail: `The saved video is ${actual.toFixed(3)}s, but the committed timeline is ${expectedDurationS.toFixed(3)}s.`,
      actualDurationS: actual,
    };
  }
  return { ok: true, actualDurationS: actual };
}
