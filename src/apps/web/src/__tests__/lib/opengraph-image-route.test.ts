/** @jest-environment node */

import { ImageResponse } from "next/og";

import { GET } from "@/app/opengraph-image/v1/route";
import {
  SOCIAL_IMAGE_PATH,
  SOCIAL_IMAGE_SIZE,
  SOCIAL_IMAGE_VERSION,
} from "@/lib/site-metadata";

jest.mock("next/og", () => ({
  ImageResponse: jest.fn(
    (_content: React.ReactNode, options: { width: number; height: number }) =>
      new Response(Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]), {
        headers: { "content-type": "image/png" },
      }),
  ),
}));

describe("/opengraph-image/v1", () => {
  it("uses a content-versioned URL for immutable social caches", () => {
    expect(SOCIAL_IMAGE_VERSION).toBe("v1");
    expect(SOCIAL_IMAGE_PATH).toBe("/opengraph-image/v1");
  });

  it("constructs a 1200×630 PNG response", async () => {
    const response = await GET();
    const bytes = new Uint8Array(await response.arrayBuffer());

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("image/png");
    expect(Array.from(bytes.slice(0, 8))).toEqual([
      137, 80, 78, 71, 13, 10, 26, 10,
    ]);
    expect(ImageResponse).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining(SOCIAL_IMAGE_SIZE),
    );
  });
});
