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

  return (
    <main className="min-h-screen bg-[#fafaf8] text-[#0c0c0e]">
      {/* ── HERO ── */}
      <FadeInOnScroll>
        <section className="mx-auto max-w-[900px] px-6 pb-0 pt-24 text-center">
          <p className="mb-5 text-[11px] font-semibold uppercase tracking-[0.24em] text-lime-600">
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
          <span className="mt-3 block text-[12.5px] text-[#71717a]">
            Eight questions about you. Three minutes. A month of content,
            scripted.
          </span>
        </section>
      </FadeInOnScroll>

      {/* ── VIDEO MARQUEE ── */}
      <section
        className="mt-[72px] flex items-end gap-[18px] overflow-x-auto md:overflow-x-hidden px-9 pb-0 touch-pan-x"
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
              and tells you{" "}
              <em className="italic text-lime-600">what to film.</em>
            </h2>
            <p className="mt-3 text-sm text-[#71717a]">
              The more Nova knows about you, the more specific your plan gets.
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
                It gets to{" "}
                <em className="italic text-lime-600">know you.</em>
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
                  It knows you
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
                It writes your{" "}
                <em className="italic text-lime-600">month.</em>
              </h3>
              <p className="text-[15px] leading-relaxed text-[#71717a]">
                Thirty days of video ideas from your actual life — filmable
                moments laid out on a calendar. Not recycled hooks.
              </p>
            </div>
            <div className="md:flex-1">
              <div className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
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
                            ? "border border-lime-200 bg-lime-50 text-lime-800"
                            : "bg-zinc-100 text-[#a1a1aa]"
                      }`}
                    >
                      <span>{d}</span>
                      {state === "done" && (
                        <span className="text-[7px]">✓</span>
                      )}
                      {state === "planned" && (
                        <span className="text-[7px] opacity-70">film</span>
                      )}
                    </div>
                  ))}
                </div>
                <div className="flex justify-between text-[10px] text-[#a1a1aa]">
                  <span className="text-lime-600">12 shoot days planned</span>
                  <span>18 remaining</span>
                </div>
              </div>
            </div>
          </div>
        </FadeInOnScroll>

        {/* Step 3 — text left, shot list right */}
        <FadeInOnScroll>
          <div className="flex flex-col gap-10 pt-16 md:flex-row md:items-center md:gap-16">
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
                      <span className="shrink-0 text-[10px] text-lime-600">
                        {dur}
                      </span>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-[10px] text-[#a1a1aa]">
                  Est. filming time:{" "}
                  <span className="text-lime-600">~8 min</span>
                </p>
              </div>
            </div>
          </div>
        </FadeInOnScroll>

        {/* Outro */}
        <div className="mt-16 text-center">
          <p className="font-display text-[22px] font-medium text-[#0c0c0e]">
            Then it edits{" "}
            <em className="italic text-lime-600">everything</em> you filmed —
            and learns what worked.
          </p>
          <p className="mt-2 text-[13px] text-[#71717a]">
            Music, pacing, text overlays. Several finished versions per shoot —
            pick one, post it.
          </p>
        </div>
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
          <em className="italic text-lime-600">what to post.</em>
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
