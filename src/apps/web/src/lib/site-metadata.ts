import type { Metadata } from "next";

import { BRAND_NAME, CANONICAL_WEB_ORIGIN } from "@/lib/brand";

export const SOCIAL_IMAGE_VERSION = "v1";
export const SOCIAL_IMAGE_PATH = `/opengraph-image/${SOCIAL_IMAGE_VERSION}`;
export const SOCIAL_IMAGE_URL = `${CANONICAL_WEB_ORIGIN}${SOCIAL_IMAGE_PATH}`;
export const SOCIAL_IMAGE_SIZE = {
  width: 1200,
  height: 630,
} as const;
export const SOCIAL_IMAGE_ALT =
  "Kria — your AI content agent for planning and editing short-form video";

const PRIVATE_ROBOTS: Metadata["robots"] = {
  index: false,
  follow: false,
  googleBot: {
    index: false,
    follow: false,
  },
};

type RouteMetadataInput = {
  title: string;
  description: string;
};

type PublicRouteMetadataInput = RouteMetadataInput & {
  path: "/" | "/generative";
};

export function publicRouteMetadata({
  title,
  description,
  path,
}: PublicRouteMetadataInput): Metadata {
  const url = new URL(path, CANONICAL_WEB_ORIGIN).toString();

  return {
    title,
    description,
    alternates: {
      canonical: url,
    },
    robots: {
      index: true,
      follow: true,
    },
    openGraph: {
      title,
      description,
      url,
      siteName: BRAND_NAME,
      type: "website",
      images: [
        {
          url: SOCIAL_IMAGE_URL,
          ...SOCIAL_IMAGE_SIZE,
          alt: SOCIAL_IMAGE_ALT,
        },
      ],
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: [
        {
          url: SOCIAL_IMAGE_URL,
          alt: SOCIAL_IMAGE_ALT,
        },
      ],
    },
  };
}

export function privateRouteMetadata({
  title,
  description,
}: RouteMetadataInput): Metadata {
  return {
    title,
    description,
    robots: PRIVATE_ROBOTS,
    // Private pages must not inherit the root landing page's canonical or
    // social-preview tags through Next.js metadata merging.
    alternates: {
      canonical: null,
    },
    openGraph: null,
    twitter: null,
  };
}

function brandedTitle(stem: string) {
  return `${stem} — ${BRAND_NAME}`;
}

export const ROUTE_METADATA = {
  landing: publicRouteMetadata({
    title: `${BRAND_NAME} — Your AI content agent`,
    description:
      "An AI agent that gives you video ideas, tells you what to film, and edits every video. You just press record.",
    path: "/",
  }),
  generative: publicRouteMetadata({
    title: brandedTitle("Make Your Edit"),
    description:
      "Upload your clips and let Kria create ready-to-share video edits.",
    path: "/generative",
  }),
  plan: privateRouteMetadata({
    title: brandedTitle("Your Plan"),
    description: "Build and manage your personalized content plan with Kria.",
  }),
  persona: privateRouteMetadata({
    title: brandedTitle("Your Persona"),
    description:
      "Review and refine the creator persona that guides your Kria content plan.",
  }),
  style: privateRouteMetadata({
    title: brandedTitle("Your Style"),
    description:
      "Review and refine the visual style Kria uses for your content.",
  }),
  planItem: privateRouteMetadata({
    title: brandedTitle("Your Video"),
    description: "Upload footage, review variants, and finish your Kria video.",
  }),
  editor: privateRouteMetadata({
    title: brandedTitle("Video Editor"),
    description: "Edit your video's clips, text, audio, and timing in Kria.",
  }),
  transcript: privateRouteMetadata({
    title: brandedTitle("Script & Record"),
    description:
      "Write your script, record your take, and review your Kria video.",
  }),
  library: privateRouteMetadata({
    title: brandedTitle("Your Videos"),
    description:
      "Review, download, and organize the videos Kria created for you.",
  }),
  renderStatus: privateRouteMetadata({
    title: brandedTitle("Render Status"),
    description:
      "Follow your video render and download the finished result.",
  }),
} satisfies Record<string, Metadata>;
