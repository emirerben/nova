import { CreationThreadError } from "@/lib/creation-thread-api";
import { creationLoadFailureCopy } from "@/lib/creation-load-failure-copy";

function problemError(
  status: number,
  code: string,
  overrides: { retryable?: boolean } = {},
): CreationThreadError {
  return new CreationThreadError("message", status, {
    code,
    phase: "accept",
    message: "message",
    trace_id: "trace-1",
    retryable: overrides.retryable,
  });
}

describe("creationLoadFailureCopy", () => {
  it("classifies offline as retryable, regardless of the cause", () => {
    expect(creationLoadFailureCopy(new Error("network"), { online: false }).tone).toBe(
      "retryable",
    );
    expect(
      creationLoadFailureCopy(problemError(404, "thread_deleted"), { online: false }).tone,
    ).toBe("retryable");
  });

  it("classifies a 401 as an auth failure, not a deletion", () => {
    const failure = creationLoadFailureCopy(new CreationThreadError("nope", 401));
    expect(failure.tone).toBe("auth");
  });

  it("classifies a confirmed deletion tombstone as terminal", () => {
    const failure = creationLoadFailureCopy(problemError(404, "thread_deleted"));
    expect(failure.tone).toBe("terminal");
    expect(failure.title.toLowerCase()).toContain("deleted");
  });

  it("classifies a genuinely missing/malformed thread id as terminal", () => {
    expect(creationLoadFailureCopy(problemError(404, "thread_not_found")).tone).toBe("terminal");
    expect(creationLoadFailureCopy(problemError(404, "thread_id_invalid")).tone).toBe("terminal");
  });

  it("classifies a service-shaped 404 (a runtime-flag rollback) as retryable, not terminal", () => {
    // Regression guard: this used to be indistinguishable from "deleted".
    const failure = creationLoadFailureCopy(problemError(404, "kria_runtime_unavailable"));
    expect(failure.tone).toBe("retryable");
  });

  it("classifies a code-less 404 as retryable -- never asserts deletion without evidence", () => {
    const failure = creationLoadFailureCopy(new CreationThreadError("missing", 404));
    expect(failure.tone).toBe("retryable");
  });

  it("classifies 5xx and the proxy's own boundary codes as retryable", () => {
    expect(creationLoadFailureCopy(new CreationThreadError("down", 503)).tone).toBe("retryable");
    expect(
      creationLoadFailureCopy(
        new CreationThreadError("Kria couldn’t reach the video service.", 502, undefined, {
          code: "upstream_unavailable",
          retryable: true,
        }),
      ).tone,
    ).toBe("retryable");
    expect(
      creationLoadFailureCopy(
        new CreationThreadError("Kria couldn’t reach the video service.", 500, undefined, {
          code: "server_misconfigured",
          retryable: false,
        }),
      ).tone,
    ).toBe("retryable");
  });

  it("falls back to the safe generic retryable copy for a non-CreationThreadError cause", () => {
    const failure = creationLoadFailureCopy(new Error("boom"));
    expect(failure.tone).toBe("retryable");
  });

  it("never returns raw error text -- only the fixed, user-safe copy set", () => {
    const failure = creationLoadFailureCopy(new CreationThreadError("raw backend stack trace", 503));
    expect(failure.detail).not.toContain("raw backend stack trace");
  });
});
