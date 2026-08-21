import {
  requestDirectionAudioUploadUrl,
  transcribeDirectionAudio,
} from "@/lib/plan-api";

afterEach(() => {
  jest.restoreAllMocks();
});

it("keeps direction audio on endpoints separate from final voiceover", async () => {
  const fetchMock = jest
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({
        upload_url: "https://storage.example/signed",
        gcs_path: "users/u/plan/item-1/direction-audio/note.webm",
      }),
    })
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ notes: "Keep the opening quick." }),
    });
  global.fetch = fetchMock;

  const upload = await requestDirectionAudioUploadUrl("item-1", {
    filename: "note.webm",
    content_type: "audio/webm",
    file_size_bytes: 1024,
  });
  const result = await transcribeDirectionAudio("item-1", upload.gcs_path);

  expect(result.notes).toBe("Keep the opening quick.");
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/plan/plan-items/item-1/direction-audio/upload-url",
    "/api/plan/plan-items/item-1/direction-audio/transcribe",
  ]);
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
    gcs_path: "users/u/plan/item-1/direction-audio/note.webm",
  });
});
