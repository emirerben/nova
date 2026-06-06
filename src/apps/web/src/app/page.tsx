import Link from "next/link";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { redirect } from "next/navigation";
import FadeInOnScroll from "@/components/FadeInOnScroll";

export const dynamic = "force-dynamic";

const SHOWCASE_CLIPS = [
  { title: "a week of mornings in my studio", from: "#32230d", to: "#0a0805" },
  { title: "what i actually eat as a med student", from: "#11202b", to: "#05080a" },
  { title: "POV: your first gallery show", from: "#2b1118", to: "#0a0507" },
  { title: "everything i packed for tokyo", from: "#1a2415", to: "#060805" },
  { title: "closing the shop at midnight", from: "#241a2e", to: "#080609" },
  { title: "my 5am open, sped up", from: "#0d2420", to: "#050807" },
] as const;

const NARRATIVE = [
  {
    num: "01",
    prefix: "It gets to ",
    italic: "know you.",
    body: "Eight short questions — your work, your people, your obsessions. Nova builds a creator persona that sounds like you, not a trend feed.",
  },
  {
    num: "02",
    prefix: "It writes your ",
    italic: "month.",
    body: "Thirty days of video ideas made for your actual life. Real, filmable moments — not recycled hooks.",
  },
  {
    num: "03",
    prefix: "It tells you what to ",
    italic: "film.",
    body: "Every day comes with a shot list and a script. Open your phone, film the three shots it asks for, upload.",
  },
  {
    num: "04",
    prefix: "It edits ",
    italic: "everything.",
    body: "Music, pacing, text overlays — your footage comes back as several finished versions. Pick one, post it.",
  },
  {
    num: "05",
    prefix: "It learns what ",
    italic: "works.",
    body: "Your videos live in one library. Tell Nova what resonated — next month's plan gets sharper.",
  },
] as const;

type CalCell = { d: string; state: "done" | "planned" | "empty" };

const CAL_CELLS: CalCell[] = [
  { d: "1", state: "done" },
  { d: "2", state: "done" },
  { d: "3", state: "planned" },
  { d: "4", state: "empty" },
  { d: "5", state: "planned" },
  { d: "6", state: "empty" },
  { d: "7", state: "planned" },
  { d: "8", state: "planned" },
  { d: "9", state: "empty" },
  { d: "10", state: "planned" },
  { d: "11", state: "empty" },
  { d: "12", state: "planned" },
  { d: "13", state: "empty" },
  { d: "14", state: "planned" },
  { d: "15", state: "empty" },
  { d: "16", state: "planned" },
  { d: "17", state: "empty" },
  { d: "18", state: "planned" },
  { d: "19", state: "empty" },
  { d: "20", state: "planned" },
  { d: "21", state: "empty" },
];

export default async function HomePage() {
  const session = await getServerSession(authOptions);
  if (session) redirect("/plan");

  return (
    <main className="min-h-screen bg-[#fafaf8] text-[#0c0c0e]">
      {/* ── HERO ── */}
      <FadeInOnScroll>
        <section className="mx-auto max-w-[900px] px-6 pb-0 pt-24 text-center">
          <p className="mb-5 text-[11px] font-semibold uppercase tracking-[0.24em] text-amber-600">
            Your AI influencer agent
          </p>
          <h1 className="font-display mb-5 text-[clamp(36px,6vw,64px)] font-medium leading-[1.08]">
            You film.
            <br />
            Your agent does{" "}
            <em className="not-italic text-amber-600">the rest.</em>
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
          <span className="mt-3 block text-[12.5px] text-[#71717a]">
            Eight questions about you. Three minutes. A month of content,
            scripted.
          </span>
        </section>
      </FadeInOnScroll>

      {/* ── VIDEO MARQUEE ── */}
      <section
        className="mt-[72px] flex items-end gap-[18px] overflow-x-auto px-9 pb-0 touch-pan-x"
        aria-label="Videos created by Nova"
      >
        {SHOWCASE_CLIPS.map((clip, i) => (
          <div
            key={clip.title}
            role="img"
            aria-label={`${clip.title} — edited by Nova`}
            className={`relative aspect-[9/16] shrink-0 overflow-hidden rounded-[18px] border border-zinc-200 shadow-[0_4px_20px_rgba(0,0,0,0.08)] md:flex-1 ${
              i % 2 === 0 ? "translate-y-7" : ""
            }`}
            style={{
              background: `linear-gradient(165deg, ${clip.from}, ${clip.to})`,
              minWidth: "clamp(100px, 28vw, 180px)",
            }}
          >
            <span className="absolute left-[15px] right-[15px] top-[26px] font-display text-[15px] italic leading-snug text-white">
              {clip.title}
            </span>
            <span className="absolute bottom-[14px] left-[15px] text-[9px] uppercase tracking-[0.14em] text-white/50">
              edited by nova
            </span>
          </div>
        ))}
      </section>
      <p className="mt-[52px] text-center text-[11.5px] uppercase tracking-[0.2em] text-[#a1a1aa]">
        Created by Nova — real videos, edited by the agent
      </p>

      {/* ── DESK SECTION ── */}
      <div className="mt-[72px] border-y border-zinc-200 bg-white px-6 py-24 md:px-12">
        <FadeInOnScroll>
          <div className="mb-16 text-center">
            <p className="mb-4 text-[11px] uppercase tracking-[0.22em] text-[#a1a1aa]">
              What your agent does
            </p>
            <h2 className="font-display text-[36px] font-medium">
              Here&apos;s what&apos;s on{" "}
              <em className="italic text-amber-600">its desk.</em>
            </h2>
            <p className="mt-2 text-sm text-[#71717a]">
              The more Nova knows about you, the more specific your plan gets.
            </p>
          </div>
        </FadeInOnScroll>

        <div className="flex flex-col items-stretch gap-6 md:flex-row md:items-center md:justify-center md:gap-5">
          {/* Persona card */}
          <div className="w-full rounded-2xl border border-zinc-200 bg-[#fafaf8] p-5 text-xs shadow-sm md:w-[260px] md:-rotate-[3deg] md:translate-y-[10px]">
            <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]">
              It knows you
            </p>
            <div className="mb-2 max-w-[90%] rounded-[10px] bg-zinc-100 px-3 py-2 leading-relaxed text-[#71717a]">
              What could you talk about for hours?
            </div>
            <div className="mb-2 ml-auto max-w-[75%] rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 leading-relaxed text-amber-800">
              third-wave coffee &amp; where to find it
            </div>
            <div className="mb-2 max-w-[90%] rounded-[10px] bg-zinc-100 px-3 py-2 leading-relaxed text-[#71717a]">
              Who do you spend your time with?
            </div>
            <div className="ml-auto max-w-[75%] rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 leading-relaxed text-amber-800">
              two roommates, one espresso machine
            </div>
          </div>

          {/* Calendar card */}
          <div className="z-10 w-full rounded-2xl border border-zinc-200 bg-white p-5 text-xs shadow-sm md:w-[400px]">
            <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]">
              Your June, planned
            </p>
            <div className="mb-2 grid grid-cols-7 gap-[5px]">
              {CAL_CELLS.map(({ d, state }) => (
                <div
                  key={d}
                  className={`flex aspect-square flex-col items-center justify-center gap-[1px] rounded-[7px] text-[9px] ${
                    state === "done"
                      ? "bg-zinc-200 text-[#3f3f46]"
                      : state === "planned"
                        ? "border border-amber-200 bg-amber-50 text-amber-800"
                        : "bg-zinc-100 text-[#a1a1aa]"
                  }`}
                >
                  <span>{d}</span>
                  {state === "done" && <span className="text-[7px]">✓</span>}
                  {state === "planned" && (
                    <span className="text-[7px] opacity-70">film</span>
                  )}
                </div>
              ))}
            </div>
            <div className="flex justify-between text-[10px] text-[#a1a1aa]">
              <span className="text-amber-600">12 shoot days planned</span>
              <span>18 remaining</span>
            </div>
          </div>

          {/* Shot list card */}
          <div className="w-full rounded-2xl border border-zinc-200 bg-[#fafaf8] p-5 text-xs shadow-sm md:w-[260px] md:rotate-[3deg] md:translate-y-[14px]">
            <p className="mb-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#a1a1aa]">
              Day 3 — shot list
            </p>
            <div className="divide-y divide-dashed divide-zinc-200">
              {[
                { b: "Shot 1", t: "order at the counter, hold on the pour" },
                { b: "Shot 2", t: "first sip, react honestly" },
                { b: "Shot 3", t: "walk-out, storefront in frame" },
              ].map(({ b, t }) => (
                <div
                  key={b}
                  className="flex gap-2 py-1.5 leading-relaxed text-[#71717a]"
                >
                  <b className="min-w-[46px] shrink-0 text-[10.5px] font-semibold text-[#0c0c0e]">
                    {b}
                  </b>
                  {t}
                </div>
              ))}
            </div>
            <p className="mt-3 text-[10px] text-[#a1a1aa]">
              Est. filming time:{" "}
              <span className="text-amber-600">~8 min</span>
            </p>
          </div>
        </div>
      </div>

      {/* ── NARRATIVE LIST ── */}
      <div className="mx-auto max-w-[800px] px-6 pb-5 pt-24 md:px-12">
        {NARRATIVE.map(({ num, prefix, italic, body }, i) => (
          <FadeInOnScroll key={num} delay={`${i * 40}ms`}>
            <div className="grid items-baseline gap-10 border-b border-zinc-200 py-12 last:border-b-0 md:grid-cols-[100px_1fr]">
              <span className="font-display text-[44px] italic text-zinc-300">
                {num}
              </span>
              <div>
                <h3 className="font-display mb-3 text-[28px] font-medium leading-snug">
                  {prefix}
                  <em className="italic text-amber-600">{italic}</em>
                </h3>
                <p className="max-w-[540px] text-[15px] leading-relaxed text-[#71717a]">
                  {body}
                </p>
              </div>
            </div>
          </FadeInOnScroll>
        ))}
      </div>

      {/* ── PROOF STRIP ── */}
      <section className="border-y border-zinc-200 bg-white px-6 py-20 md:px-12">
        <div className="flex flex-col items-center gap-10 text-center md:flex-row md:justify-center md:gap-[72px]">
          {[
            { big: "3 min", lbl: "to your first plan" },
            { big: "30 days", lbl: "scripted at once" },
            { big: "~10 min", lbl: "of filming per day" },
          ].map(({ big, lbl }) => (
            <div key={lbl}>
              <p className="font-display text-[36px] font-medium text-[#0c0c0e]">
                {big}
              </p>
              <p className="mt-1.5 text-[11px] uppercase tracking-[0.15em] text-[#a1a1aa]">
                {lbl}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* ── CLOSING CTA ── */}
      <section className="px-6 py-[110px] text-center md:px-12">
        <h2 className="font-display mb-3 text-[44px] font-medium leading-snug">
          Stop guessing{" "}
          <em className="italic text-amber-600">what to post.</em>
        </h2>
        <p className="mb-7 text-[15px] text-[#71717a]">
          Your agent already knows.
        </p>
        <Link
          href="/plan"
          className="inline-block rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80"
        >
          Build my plan
        </Link>
      </section>

      {/* ── FOOTER ── */}
      <footer className="flex items-center justify-between border-t border-zinc-200 bg-white px-6 py-8 text-[13px] text-[#a1a1aa] md:px-12">
        <span>© Nova</span>
        <nav className="flex gap-6" aria-label="Footer navigation">
          <Link
            href="/templates"
            className="transition-colors hover:text-[#0c0c0e]"
          >
            Templates
          </Link>
          <Link
            href="/library"
            className="transition-colors hover:text-[#0c0c0e]"
          >
            Library
          </Link>
          <Link
            href="/api/auth/signin"
            className="transition-colors hover:text-[#0c0c0e]"
          >
            Sign in
          </Link>
        </nav>
      </footer>
    </main>
  );
}
