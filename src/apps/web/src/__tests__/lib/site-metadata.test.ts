import { CANONICAL_WEB_ORIGIN } from "@/lib/brand";
import {
  privateRouteMetadata,
  publicRouteMetadata,
  ROUTE_METADATA,
  SOCIAL_IMAGE_SIZE,
  SOCIAL_IMAGE_URL,
} from "@/lib/site-metadata";

describe("site metadata", () => {
  it("builds absolute canonical and social-image URLs for public routes", () => {
    const metadata = publicRouteMetadata({
      title: "Make Your Edit — Kria",
      description: "Create a ready-to-share edit.",
      path: "/generative",
    });

    expect(metadata.alternates).toEqual({
      canonical: `${CANONICAL_WEB_ORIGIN}/generative`,
    });
    expect(metadata.robots).toEqual({ index: true, follow: true });
    expect(metadata.openGraph).toMatchObject({
      title: "Make Your Edit — Kria",
      description: "Create a ready-to-share edit.",
      url: `${CANONICAL_WEB_ORIGIN}/generative`,
      images: [
        {
          url: SOCIAL_IMAGE_URL,
          ...SOCIAL_IMAGE_SIZE,
        },
      ],
    });
    expect(metadata.twitter).toMatchObject({
      card: "summary_large_image",
      images: [{ url: SOCIAL_IMAGE_URL }],
    });
  });

  it("prevents private routes from inheriting public canonical and share tags", () => {
    const metadata = privateRouteMetadata({
      title: "Your Plan — Kria",
      description: "Build your personalized content plan.",
    });

    expect(metadata.robots).toMatchObject({
      index: false,
      follow: false,
      googleBot: {
        index: false,
        follow: false,
      },
    });
    expect(metadata.alternates).toEqual({ canonical: null });
    expect(metadata.openGraph).toBeNull();
    expect(metadata.twitter).toBeNull();
  });

  it("matches every approved customer-route title and indexing policy", () => {
    const expected = {
      landing: ["Kria — Your AI content agent", true],
      generative: ["Make Your Edit — Kria", true],
      plan: ["Your Plan — Kria", false],
      persona: ["Your Persona — Kria", false],
      style: ["Your Style — Kria", false],
      planItem: ["Your Video — Kria", false],
      editor: ["Video Editor — Kria", false],
      transcript: ["Script & Record — Kria", false],
      library: ["Your Videos — Kria", false],
      renderStatus: ["Render Status — Kria", false],
    } as const;

    for (const [owner, [title, index]] of Object.entries(expected)) {
      const metadata = ROUTE_METADATA[owner as keyof typeof ROUTE_METADATA];
      expect(metadata.title).toBe(title);
      expect(metadata.robots).toMatchObject({ index, follow: index });

      if (index) {
        expect(metadata.alternates).toMatchObject({
          canonical: expect.stringMatching(/^https:\/\/www\.usekria\.com/),
        });
        expect(metadata.openGraph).not.toBeNull();
        expect(metadata.twitter).not.toBeNull();
      } else {
        expect(metadata.alternates).toEqual({ canonical: null });
        expect(metadata.openGraph).toBeNull();
        expect(metadata.twitter).toBeNull();
      }
    }
  });
});
