import Link from "next/link";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { redirect } from "next/navigation";
import FadeInOnScroll from "@/components/FadeInOnScroll";
import ShowcaseMarquee from "@/components/ShowcaseMarquee";

export const dynamic = "force-dynamic";

// ── SHOWCASE CLIPS ────────────────────────────────────────────────────────────
// To add real videos:
//   1. gsutil cp <output.mp4> gs://$STORAGE_BUCKET/landing/<slug>.mp4
//      (no ACL grant needed — UBLA + /landing-clips endpoint handles auth)
//   2. Add the GCS object path as `key` below (e.g. "landing/my-clip.mp4")
//   3. The server fetches a fresh signed URL at render time via GET /landing-clips
//
// Clips without a `key` fall back to their CSS gradient (current state).
// Keep clips ~720×1280 H.264, ≤ ~5 MB each, preload="metadata".
// ─────────────────────────────────────────────────────────────────────────────
const SHOWCASE_CLIPS = [
  { title: "a week of mornings in my studio", from: "#32230d", to: "#0a0805", key: "landing/clip-overnight.mp4" },
  { title: "what i actually eat as a med student", from: "#11202b", to: "#05080a", key: "landing/clip-bad-bunny.mp4" },
  { title: "POV: your first gallery show", from: "#2b1118", to: "#0a0507", key: "landing/clip-montagem.mp4" },
  { title: "everything i packed for tokyo", from: "#1a2415", to: "#060805", key: "landing/clip-again.mp4" },
  { title: "closing the shop at midnight", from: "#241a2e", to: "#080609", key: "landing/clip-travis.mp4" },
  { title: "my 5am open, sped up", from: "#0d2420", to: "#050807", key: "landing/clip-success.mp4" },
] satisfies { title: string; from: string; to: string; key?: string }[];

// Resolve signed URLs for any clips that have a GCS key.
// Server-side only (force-dynamic) — fresh URL every render, never stale.
async function resolveClipUrls(
  clips: { title: string; from: string; to: string; key?: string }[],
): Promise<{ title: string; from: string; to: string; src?: string }[]> {
  const keys = clips.map((c) => c.key).filter(Boolean) as string[];
  if (keys.length === 0) return clips;

  const apiBase =
    process.env.API_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    "http://localhost:8000";
  const qs = keys.map((k) => `keys=${encodeURIComponent(k)}`).join("&");
  try {
    const res = await fetch(`${apiBase}/landing-clips?${qs}`, {
      cache: "no-store",
    });
    if (!res.ok) return clips;
    const signed: { key: string; src: string | null }[] = await res.json();
    const srcByKey = Object.fromEntries(
      signed.map(({ key, src }) => [key, src ?? undefined]),
    );
    return clips.map((c) => ({ ...c, src: c.key ? srcByKey[c.key] : undefined }));
  } catch {
    // Best-effort: if the backend is unreachable, fall back to gradients.
    return clips;
  }
}

type CalCell = { d: string; state: "done" | "film" | "post" | "empty" };

const CAL_CELLS: CalCell[] = [
  { d: "1", state: "done" },
  { d: "2", state: "done" },
  { d: "3", state: "film" },
  { d: "4", state: "post" },
  { d: "5", state: "post" },
  { d: "6", state: "post" },
  { d: "7", state: "film" },
  { d: "8", state: "post" },
  { d: "9", state: "post" },
  { d: "10", state: "post" },
  { d: "11", state: "film" },
  { d: "12", state: "post" },
  { d: "13", state: "post" },
  { d: "14", state: "post" },
  { d: "15", state: "post" },
  { d: "16", state: "post" },
  { d: "17", state: "post" },
  { d: "18", state: "empty" },
  { d: "19", state: "empty" },
  { d: "20", state: "empty" },
  { d: "21", state: "empty" },
];

const INTERVIEW = [
  {
    q: "What do you do for work?",
    a: "i manage a café, open most mornings",
  },
  {
    q: "What could you talk about for hours?",
    a: "specialty coffee and where to find it",
  },
] as const;

const SHOTS = [
  {
    b: "Shot 1",
    t: "unlocking the front door, streetlight still on",
    how: "handheld, eye level",
    dur: "6s",
  },
  {
    b: "Shot 2",
    t: "first espresso of the day pulling",
    how: "close-up on the cup",
    dur: "4s",
  },
  {
    b: "Shot 3",
    t: "flipping the open sign",
    how: "phone propped on the counter",
    dur: "5s",
  },
] as const;

export default async function HomePage() {
  const session = await getServerSession(authOptions);
  if (session) redirect("/plan");

  const resolvedClips = await resolveClipUrls(SHOWCASE_CLIPS);

  return (
    <main className="min-h-screen bg-[#fafaf8] text-[#0c0c0e]">
      {/* ── HERO ── */}
      <FadeInOnScroll>
        <section className="mx-auto max-w-[900px] px-6 pb-0 pt-24 text-center">
          <p className="mb-5 text-[11px] font-semibold uppercase tracking-[0.24em] text-lime-700">
            Your AI influencer agent
          </p>
          <h1 className="font-display mb-5 text-[clamp(36px,6vw,64px)] font-medium leading-[1.08]">
            You film.
            <br />
            Your agent does{" "}
            <em className="not-italic text-lime-600">the rest.</em>
          </h1>
          <p className="mx-auto mb-9 max-w-[500px] text-[17px] leading-relaxed text-[#71717a]">
            Nova plans your month, scripts every day, edits your footage, and
            learns what works. Your content career, on autopilot.
          </p>
          <Link
            href="/plan"
            className="inline-block rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80"
          >
            Build my plan
          </Link>
        </section>
      </FadeInOnScroll>

      {/* ── VIDEO MARQUEE ── */}
      <ShowcaseMarquee clips={resolvedClips} />

      {/* ── PROCESS SECTION ── */}
      <div className="mt-[72px] border-y border-zinc-200 bg-white px-6 py-24 md:px-16">
        <FadeInOnScroll>
          <div className="mb-20 text-center">
            <p className="mb-4 text-[11px] uppercase tracking-[0.22em] text-[#a1a1aa]">
              How your agent works
            </p>
            <h2 className="font-display text-[36px] font-medium leading-snug">
              It learns you, plans your month,
              <br />
              tells you{" "}
              <em className="italic text-lime-600">what to film,</em>
              <br />
              then edits{" "}
              <em className="italic text-lime-600">everything.</em>
            </h2>
            <p className="mt-3 text-sm text-[#71717a]">
              The more Nova learns about you, the more specific your plan gets.
            </p>
          </div>
        </FadeInOnScroll>

        {/* Step 1 — text left, interview card right */}
        <FadeInOnScroll>
          <div className="flex flex-col gap-10 border-b border-zinc-100 pb-16 md:flex-row md:items-center md:gap-16">
            <div className="md:flex-1">
              <span className="font-display text-[44px] italic text-zinc-200">
                01
              </span>
              <h3 className="font-display mb-3 mt-1 text-[28px] font-medium leading-snug">
                It{" "}
                <em className="italic text-lime-600">learns about you.</em>
              </h3>
              <p className="text-[15px] leading-relaxed text-[#71717a]">
                Eight short questions — your work, your people, the things you
                could talk about for hours. Your agent builds a creator persona
                that sounds like you, not a trend feed.
              </p>
            </div>
            <div className="md:flex-1">
              <div className="rounded-2xl border border-zinc-200 bg-[#fafaf8] p-5 shadow-sm">
                <p className="mb-4 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]">
                  It learns about you
                </p>
                {INTERVIEW.map(({ q, a }) => (
                  <div key={q} className="mb-4 last:mb-0">
                    <p className="font-display mb-2 text-[16px] leading-snug text-[#0c0c0e]">
                      {q}
                    </p>
                    <div className="border-l-2 border-lime-500 pl-3">
                      <p className="text-[10px] uppercase tracking-[0.12em] text-[#a1a1aa]">
                        you said
                      </p>
                      <p className="text-[13px] text-[#71717a]">{a}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </FadeInOnScroll>

        {/* Step 2 — calendar left, text right (reversed on desktop) */}
        <FadeInOnScroll>
          <div className="flex flex-col gap-10 border-b border-zinc-100 py-16 md:flex-row-reverse md:items-center md:gap-16">
            <div className="md:flex-1">
              <span className="font-display text-[44px] italic text-zinc-200">
                02
              </span>
              <h3 className="font-display mb-3 mt-1 text-[28px] font-medium leading-snug">
                It gives you{" "}
                <em className="italic text-lime-600">ideas.</em>
              </h3>
              <p className="text-[15px] leading-relaxed text-[#71717a]">
                Video ideas from your actual life — filmable moments laid out
                so you always know what to film. Not recycled hooks. Every
                post is its own idea.
              </p>
            </div>
            <div className="md:flex-1">
              <div className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
                <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]">
                  Your June, planned
                </p>
                {/* Legend */}
                <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <span className="flex items-center gap-1 text-[10px] text-[#a1a1aa]">
                    <span className="inline-block h-2 w-2 rounded-[2px] border border-lime-200 bg-lime-50" />
                    film day
                  </span>
                  <span className="flex items-center gap-1 text-[10px] text-[#a1a1aa]">
                    <span className="inline-block h-2 w-2 rounded-[2px] bg-lime-600" />
                    post day
                  </span>
                  <span className="flex items-center gap-1 text-[10px] text-[#a1a1aa]">
                    <span className="inline-block h-2 w-2 rounded-[2px] bg-zinc-200" />
                    done
                  </span>
                </div>
                <div className="mb-2 grid grid-cols-7 gap-[5px]">
                  {CAL_CELLS.map(({ d, state }) => (
                    <div
                      key={d}
                      className={`flex aspect-square flex-col items-center justify-center rounded-[7px] text-[9px] ${
                        state === "done"
                          ? "bg-zinc-200 text-[#3f3f46]"
                          : state === "film"
                            ? "border border-lime-200 bg-lime-50 text-lime-800"
                            : state === "post"
                              ? "bg-lime-600 text-white"
                              : "bg-zinc-100 text-[#a1a1aa]"
                      }`}
                    >
                      <span>{d}</span>
                      {state === "done" && (
                        <span className="text-[7px]">✓</span>
                      )}
                    </div>
                  ))}
                </div>
                <div className="flex justify-between text-[10px] text-[#a1a1aa]">
                  <span className="text-lime-700">3 film days → 11 posts</span>
                  <span>each post is its own video</span>
                </div>
              </div>
            </div>
          </div>
        </FadeInOnScroll>

        {/* Step 3 — text left, shot list right */}
        <FadeInOnScroll>
          <div className="flex flex-col gap-10 border-b border-zinc-100 pt-16 pb-16 md:flex-row md:items-center md:gap-16">
            <div className="md:flex-1">
              <span className="font-display text-[44px] italic text-zinc-200">
                03
              </span>
              <h3 className="font-display mb-3 mt-1 text-[28px] font-medium leading-snug">
                It tells you what to{" "}
                <em className="italic text-lime-600">film.</em>
              </h3>
              <p className="text-[15px] leading-relaxed text-[#71717a]">
                Every shoot day comes with a shot list — what to capture, how
                to frame it, how long. Open your phone, get the shots, upload.
              </p>
            </div>
            <div className="md:flex-1">
              <div className="rounded-2xl border border-zinc-200 bg-[#fafaf8] p-5 shadow-sm">
                <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]">
                  Day 3 — &ldquo;my 5am open&rdquo; · shot list
                </p>
                <div className="divide-y divide-dashed divide-zinc-200">
                  {SHOTS.map(({ b, t, how, dur }) => (
                    <div
                      key={b}
                      className="flex items-baseline gap-2 py-2 text-[#71717a]"
                    >
                      <b className="min-w-[46px] shrink-0 text-[10.5px] font-semibold text-[#0c0c0e]">
                        {b}
                      </b>
                      <span className="flex-1 text-[12px] leading-relaxed">
                        {t}
                        <span className="block text-[10.5px] text-[#a1a1aa]">
                          {how}
                        </span>
                      </span>
                      <span className="shrink-0 text-[10px] text-lime-700">
                        {dur}
                      </span>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-[10px] text-[#a1a1aa]">
                  Est. filming time:{" "}
                  <span className="text-lime-700">~8 min</span>
                </p>
              </div>
            </div>
          </div>
        </FadeInOnScroll>

        {/* Step 4 — phone fan left, text right on desktop (mirrors 02) */}
        <FadeInOnScroll>
          <div className="flex flex-col gap-10 pt-16 md:flex-row-reverse md:items-center md:gap-16">
            {/* Right on desktop: step copy */}
            <div className="md:flex-1">
              <span className="font-display text-[44px] italic text-zinc-200">
                04
              </span>
              <h3 className="font-display mb-3 mt-1 text-[28px] font-medium leading-snug">
                Then it edits{" "}
                <em className="italic text-lime-600">everything.</em>
              </h3>
              <p className="text-[15px] leading-relaxed text-[#71717a]">
                Music, pacing, text overlays — several finished versions per
                shoot, each cut a different way. Pick the one that feels like
                you, post it, and your agent learns what worked.
              </p>
            </div>

            {/* Left on desktop: fanned phone mockups — lg+ only; flat row below lg */}
            <div className="md:flex-1">
              {/* Desktop fan (lg+) */}
              <div className="hidden lg:block">
                <div className="relative mx-auto flex h-[300px] w-[440px] items-center justify-center">
                  {/* Left tile — song lyrics */}
                  <div
                    className="absolute h-[260px] w-[118px] overflow-hidden rounded-[14px] shadow-[0_12px_30px_rgba(0,0,0,0.18)]"
                    style={{
                      transform: "rotate(-7deg) translateX(-130px)",
                      background: "linear-gradient(165deg,#1a1a22,#0d0d12)",
                    }}
                    aria-hidden="true"
                  >
                    <div className="flex h-full flex-col items-center justify-center gap-1 px-3">
                      <p className="font-display text-center text-[11px] italic leading-snug text-white/40">
                        sunday morning
                      </p>
                      <p className="font-display text-center text-[11px] italic leading-snug text-lime-400">
                        i&apos;m still here
                      </p>
                      <p className="font-display text-center text-[11px] italic leading-snug text-white/40">
                        waiting on you
                      </p>
                    </div>
                  </div>
                  {/* Center tile — selected (lime outline) */}
                  <div
                    className="relative z-10 h-[260px] w-[118px] overflow-hidden rounded-[14px] shadow-[0_12px_30px_rgba(0,0,0,0.18)] outline outline-2 outline-offset-2 outline-lime-500"
                    style={{
                      background: "linear-gradient(165deg,#1a2215,#0a0f09)",
                    }}
                    aria-hidden="true"
                  >
                    <div className="flex h-full flex-col items-center justify-center px-3 text-center">
                      <p className="font-display text-[13px] font-medium leading-snug text-white">
                        POV: your agent edited all of this
                      </p>
                    </div>
                  </div>
                  {/* Right tile — caption */}
                  <div
                    className="absolute h-[260px] w-[118px] overflow-hidden rounded-[14px] shadow-[0_12px_30px_rgba(0,0,0,0.18)]"
                    style={{
                      transform: "rotate(7deg) translateX(130px)",
                      background: "linear-gradient(165deg,#221a1a,#120d0d)",
                    }}
                    aria-hidden="true"
                  >
                    <div className="flex h-full flex-col items-end justify-end px-3 pb-4">
                      <p className="font-display text-right text-[10px] italic leading-relaxed text-white/60">
                        tuesday, 7:42am — the shop opens
                      </p>
                    </div>
                  </div>
                </div>
                {/* Variant labels + pill */}
                <div className="mt-4 text-center">
                  <p className="text-[10px] uppercase tracking-[0.18em] text-[#a1a1aa]">
                    song lyrics · song text · original text
                  </p>
                  <span className="mt-3 inline-block rounded-full border border-lime-200 bg-lime-50 px-4 py-1.5 text-[12px] font-medium text-lime-800">
                    post this one
                  </span>
                </div>
              </div>

              {/* Mobile flat row (below lg) */}
              <div className="lg:hidden">
                <div className="flex items-end gap-3">
                  {/* Left tile */}
                  <div
                    className="h-[200px] w-[80px] flex-shrink-0 overflow-hidden rounded-[10px] shadow-md"
                    style={{
                      background: "linear-gradient(165deg,#1a1a22,#0d0d12)",
                    }}
                    aria-hidden="true"
                  >
                    <div className="flex h-full flex-col items-center justify-center gap-0.5 px-2">
                      <p className="font-display text-center text-[9px] italic leading-snug text-white/40">
                        sunday morning
                      </p>
                      <p className="font-display text-center text-[9px] italic leading-snug text-lime-400">
                        i&apos;m still here
                      </p>
                      <p className="font-display text-center text-[9px] italic leading-snug text-white/40">
                        waiting on you
                      </p>
                    </div>
                  </div>
                  {/* Center tile — selected */}
                  <div
                    className="h-[220px] w-[90px] flex-shrink-0 overflow-hidden rounded-[10px] shadow-md outline outline-2 outline-offset-2 outline-lime-500"
                    style={{
                      background: "linear-gradient(165deg,#1a2215,#0a0f09)",
                    }}
                    aria-hidden="true"
                  >
                    <div className="flex h-full flex-col items-center justify-center px-2 text-center">
                      <p className="font-display text-[10px] font-medium leading-snug text-white">
                        POV: your agent edited all of this
                      </p>
                    </div>
                  </div>
                  {/* Right tile */}
                  <div
                    className="h-[200px] w-[80px] flex-shrink-0 overflow-hidden rounded-[10px] shadow-md"
                    style={{
                      background: "linear-gradient(165deg,#221a1a,#120d0d)",
                    }}
                    aria-hidden="true"
                  >
                    <div className="flex h-full flex-col items-end justify-end px-2 pb-3">
                      <p className="font-display text-right text-[9px] italic leading-relaxed text-white/60">
                        tuesday, 7:42am — the shop opens
                      </p>
                    </div>
                  </div>
                </div>
                {/* Variant labels + pill */}
                <div className="mt-3">
                  <p className="text-[10px] uppercase tracking-[0.18em] text-[#a1a1aa]">
                    song lyrics · song text · original text
                  </p>
                  <span className="mt-2 inline-block rounded-full border border-lime-200 bg-lime-50 px-4 py-1.5 text-[12px] font-medium text-lime-800">
                    post this one
                  </span>
                </div>
              </div>
            </div>
          </div>
        </FadeInOnScroll>
      </div>

      {/* ── FOOTER ── */}
      <footer className="border-t border-zinc-200 bg-white px-6 py-8 text-center text-[13px] text-[#a1a1aa] md:px-12">
        <span>© Nova</span>
      </footer>
    </main>
  );
}
