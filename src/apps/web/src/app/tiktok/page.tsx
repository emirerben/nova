import type { Metadata } from "next";
import TikTokProductWorkspace from "./TikTokProductWorkspace";

export const dynamic = "force-dynamic";
const DEMO_VIDEO_FETCH_TIMEOUT_MS = 4_000;

export const metadata: Metadata = {
  title: "TikTok publishing workspace — Kria",
  description:
    "Explore Kria's complete TikTok Direct Post workflow: exact-video approval, creator-controlled publishing, lifecycle tracking, and performance learning.",
};

async function resolveDemoVideo(): Promise<string | null> {
  const apiBase =
    process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), DEMO_VIDEO_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(
      `${apiBase}/landing-clips?keys=${encodeURIComponent("landing/clip-overnight.mp4")}`,
      { cache: "no-store", signal: controller.signal },
    );
    if (!response.ok) return null;
    const rows = (await response.json()) as { key: string; src: string | null }[];
    return rows[0]?.src ?? null;
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

export default async function TikTokProductPage() {
  return <TikTokProductWorkspace videoSrc={await resolveDemoVideo()} />;
}
