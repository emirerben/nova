/**
 * commitEditorSession transport-failure behavior: the 20s watchdog abort and
 * plain network failures both surface as EditorCommitNetworkError (edits are
 * kept client-side), while every server-response path — curated 422 copy,
 * 409 conflict, success — stays byte-identical to before.
 */
import {
  commitEditorSession,
  EditorCommitConflictError,
  EditorCommitNetworkError,
  EDITOR_COMMIT_TIMEOUT_MS,
  type EditorCommitRequest,
} from "@/lib/editor-commit";

const request: EditorCommitRequest = { base_generation: "gen-current" };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

describe("commitEditorSession network handling", () => {
  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("aborts after EDITOR_COMMIT_TIMEOUT_MS and throws EditorCommitNetworkError", async () => {
    jest.useFakeTimers();
    const fetchMock = jest.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const promise = commitEditorSession("item-1", "var-1", request);
    const assertion = expect(promise).rejects.toMatchObject({
      name: "EditorCommitNetworkError",
      message: "Couldn't reach Kria — your edits are still here.",
    });
    jest.advanceTimersByTime(EDITOR_COMMIT_TIMEOUT_MS);
    await assertion;
    await expect(promise).rejects.toBeInstanceOf(EditorCommitNetworkError);
  });

  it("maps a transport-level TypeError to EditorCommitNetworkError", async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new TypeError("Failed to fetch")) as unknown as typeof fetch;

    const promise = commitEditorSession("item-1", "var-1", request);
    await expect(promise).rejects.toBeInstanceOf(EditorCommitNetworkError);
    await expect(promise).rejects.toThrow(
      "Couldn't reach Kria — your edits are still here.",
    );
  });

  it("keeps the curated 422 validation copy (not a network error)", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(
        jsonResponse(422, { detail: { code: "TIMELINE_BEATS_EXHAUSTED" } }),
      ) as unknown as typeof fetch;

    const promise = commitEditorSession("item-1", "var-1", request);
    await expect(promise).rejects.toThrow(
      "Ran out of song to sync clips to — try removing a clip.",
    );
    await promise.catch((err) => {
      expect(err).toBeInstanceOf(Error);
      expect(err).not.toBeInstanceOf(EditorCommitNetworkError);
      expect(err).not.toBeInstanceOf(EditorCommitConflictError);
    });
  });

  it("still throws EditorCommitConflictError on a 409", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(
        jsonResponse(409, { detail: "Baseline is stale" }),
      ) as unknown as typeof fetch;

    const promise = commitEditorSession("item-1", "var-1", request);
    await expect(promise).rejects.toBeInstanceOf(EditorCommitConflictError);
    await expect(promise).rejects.toThrow("Baseline is stale");
  });

  it("resolves on success and clears the watchdog timer", async () => {
    jest.useFakeTimers();
    const body = { ok: true, generation: "gen-next", sections: {} };
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, body));
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(
      commitEditorSession("item-1", "var-1", request),
    ).resolves.toEqual(body);
    // clearTimeout ran in the finally — no watchdog timer left pending.
    expect(jest.getTimerCount()).toBe(0);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });
});
