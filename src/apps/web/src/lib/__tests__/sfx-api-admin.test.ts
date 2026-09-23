// Admin sound-effect client must match the API contract exactly (KRI-173):
// upload-init needs ext + byte_count, the signed PUT must send the signed
// content type, PATCH uses publish/archive, and audio-url returns audio_url.
import {
  adminUploadSfx,
  getSfxAudioUrl,
  patchSoundEffect,
  sfxUploadExtension,
} from "@/lib/sfx-api";

type FetchCall = [string, RequestInit | undefined];

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => "" } as Response;
}

describe("sfx-api admin client", () => {
  const fetchMock = jest.fn();
  const putHeaders: Record<string, string>[] = [];

  beforeEach(() => {
    fetchMock.mockReset();
    putHeaders.length = 0;
    global.fetch = fetchMock as unknown as typeof fetch;
    class FakeXhr {
      status = 200;
      upload: { onprogress?: unknown } = {};
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      private headers: Record<string, string> = {};
      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers[name] = value;
      }
      send() {
        putHeaders.push(this.headers);
        this.onload?.();
      }
    }
    (global as unknown as { XMLHttpRequest: unknown }).XMLHttpRequest = FakeXhr;
  });

  it("sends ext + byte_count and PUTs with the signed content type", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          effect_id: "fx1",
          upload_url: "https://gcs/signed",
          gcs_path: "sound-effects/fx1/audio.m4a",
          content_type: "audio/mp4",
          expires_in_s: 900,
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ effect_id: "fx1", status: "ready", duration_s: 0.4 }));
    const file = new File([new Uint8Array(2048)], "Wrong Buzzer.M4A", { type: "audio/x-m4a" });

    const confirmed = await adminUploadSfx(file, "Wrong buzzer");

    const [initUrl, initInit] = fetchMock.mock.calls[0] as FetchCall;
    expect(initUrl).toBe("/api/admin/sound-effects/upload-init-file");
    expect(JSON.parse(String(initInit?.body))).toEqual({
      filename: "Wrong Buzzer.M4A",
      name: "Wrong buzzer",
      ext: ".m4a",
      byte_count: 2048,
    });
    expect(putHeaders[0]["Content-Type"]).toBe("audio/mp4");
    expect(confirmed.status).toBe("ready");
  });

  it("rejects containers the iPhone cannot play before uploading", () => {
    expect(() => sfxUploadExtension("fx.ogg")).toThrow(/iPhone/);
    expect(sfxUploadExtension("fx.wav")).toBe(".wav");
  });

  it("uses the API's publish/archive field names and audio_url", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ id: "fx1" }))
      .mockResolvedValueOnce(jsonResponse({ audio_url: "https://gcs/fx1.m4a" }));

    await patchSoundEffect("fx1", { publish: true, manual_audit_status: "approved" });
    const url = await getSfxAudioUrl("fx1");

    const [, patchInit] = fetchMock.mock.calls[0] as FetchCall;
    expect(JSON.parse(String(patchInit?.body))).toEqual({
      publish: true,
      manual_audit_status: "approved",
    });
    expect(url).toBe("https://gcs/fx1.m4a");
  });
});
