/**
 * /privacy — static Privacy Policy.
 *
 * Public route (middleware.ts matcher only covers /admin/*), no session read,
 * statically prerendered. Drafted against the privacy-policy skill's
 * 14-section template, tailored to what Kria actually collects and does —
 * see /Users/emirerben/.claude/plans/run-npx-skills-use-floating-church.md
 * (A2) for the sourcing behind each section, verified against the codebase
 * rather than assumed.
 *
 * DISCLAIMER: generated for planning purposes, not legal advice. Every
 * [⚠️ LEGAL REVIEW REQUIRED] marker below needs attorney review before this
 * page is safe to link from production. See docs/legal/README.md.
 */
import Link from "next/link";
import type { ReactNode } from "react";

import { Eyebrow } from "@/components/ui/Eyebrow";
import {
  EFFECTIVE_DATE,
  GOVERNING_LAW,
  LEGAL_ADDRESS,
  LEGAL_ENTITY,
  PRIVACY_EMAIL,
  REQUEST_RESPONSE_DAYS,
} from "@/lib/legal";

export const metadata = {
  title: "Privacy — Kria",
};

function Section({
  n,
  title,
  children,
  flag,
}: {
  n: string;
  title: string;
  children: ReactNode;
  flag?: boolean;
}) {
  return (
    <section id={`s${n}`} className="scroll-mt-20 border-t border-zinc-100 py-9 first:border-t-0 first:pt-0">
      <Eyebrow tone="muted" className="mb-3">
        {n}. {title}
        {flag && <span className="ml-2 normal-case tracking-normal text-amber-700">⚠ legal review</span>}
      </Eyebrow>
      <div className="space-y-3 text-[15px] leading-relaxed text-[#3f3f46]">{children}</div>
    </section>
  );
}

const TOC: { n: string; title: string }[] = [
  { n: "1", title: "Information We Collect" },
  { n: "2", title: "How We Collect It" },
  { n: "3", title: "How We Use It" },
  { n: "4", title: "Legal Basis for Processing (GDPR)" },
  { n: "5", title: "AI Sub-Processors & Third Parties" },
  { n: "6", title: "Your Public TikTok Profile" },
  { n: "7", title: "International Data Transfers" },
  { n: "8", title: "Data Retention" },
  { n: "9", title: "Your Rights" },
  { n: "10", title: "Cookies & Tracking" },
  { n: "11", title: "Security" },
  { n: "12", title: "Children's Privacy" },
  { n: "13", title: "Contact & Complaints" },
  { n: "14", title: "Changes to This Policy" },
];

export default function PrivacyPage() {
  return (
    <main className="min-h-screen bg-[#ffffff] text-[#0c0c0e]">
      <div className="mx-auto max-w-[680px] px-6 pb-24 pt-16">
        <Eyebrow tone="lime" className="mb-3">
          Legal
        </Eyebrow>
        <h1 className="font-display mb-2 text-[36px] font-medium leading-snug">Privacy Policy</h1>
        <p className="mb-8 text-[13px] text-[#a1a1aa]">
          Effective {EFFECTIVE_DATE}. Read alongside our{" "}
          <Link href="/terms" className="text-lime-700 underline underline-offset-2">
            Terms of Service
          </Link>
          .
        </p>

        {/* ── PART 1: SUMMARY ─────────────────────────────────────────────── */}
        <div className="mb-10 rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
          <Eyebrow tone="muted" className="mb-3">
            Summary
          </Eyebrow>
          <dl className="space-y-2.5 text-[14px] text-[#3f3f46]">
            <div>
              <dt className="text-[11px] uppercase tracking-[0.1em] text-[#a1a1aa]">Who we are</dt>
              <dd>
                Kria, operated by {LEGAL_ENTITY} — an AI content agent that turns your footage into short-form
                video.
              </dd>
            </div>
            <div>
              <dt className="text-[11px] uppercase tracking-[0.1em] text-[#a1a1aa]">What we collect</dt>
              <dd>
                Your name and email (via Google sign-in), the video/audio/images you upload, your answers to our
                onboarding questions, and — only if you give us your handle — your public TikTok profile.
              </dd>
            </div>
            <div>
              <dt className="text-[11px] uppercase tracking-[0.1em] text-[#a1a1aa]">Where it goes</dt>
              <dd>
                To Google and OpenAI, to power the editing (§5). We do not sell your data, and we run no analytics or
                advertising trackers of any kind (§10).
              </dd>
            </div>
            <div>
              <dt className="text-[11px] uppercase tracking-[0.1em] text-[#a1a1aa]">Your rights</dt>
              <dd>
                Access, correct, export, or delete your data at any time — see §9. We respond within{" "}
                {REQUEST_RESPONSE_DAYS} days.
              </dd>
            </div>
            <div>
              <dt className="text-[11px] uppercase tracking-[0.1em] text-[#a1a1aa]">Contact</dt>
              <dd>
                <a href={`mailto:${PRIVACY_EMAIL}`} className="text-lime-700 underline underline-offset-2">
                  {PRIVACY_EMAIL}
                </a>
              </dd>
            </div>
          </dl>
        </div>

        {/* ── TOC ───────────────────────────────────────────────────────── */}
        <nav className="mb-10 rounded-2xl border border-zinc-200 bg-[#ffffff] p-5">
          <Eyebrow tone="muted" className="mb-3">
            Contents
          </Eyebrow>
          <ol className="grid grid-cols-1 gap-x-6 gap-y-1 text-[13px] text-[#71717a] sm:grid-cols-2">
            {TOC.map(({ n, title }) => (
              <li key={n}>
                <a href={`#s${n}`} className="hover:text-lime-700 hover:underline">
                  {n}. {title}
                </a>
              </li>
            ))}
          </ol>
        </nav>

        <p className="mb-8 text-[14px] leading-relaxed text-[#71717a]">
          This policy describes how {LEGAL_ENTITY}, operating as Kria (&ldquo;we&rdquo;, &ldquo;us&rdquo;), collects,
          uses, and protects personal data when you use the Kria website and application (the &ldquo;Service&rdquo;).
          It is written to be specific about what Kria actually does, not a generic template — every data type below
          reflects what is in the product today.
        </p>

        {/* ── 1 ─────────────────────────────────────────────────────────── */}
        <Section n="1" title="Information We Collect">
          <p>
            <strong>Account information.</strong> When you sign in with Google, we receive and store your name and
            email address. We do not store your Google password, profile photo, or Google account ID.
          </p>
          <p>
            <strong>Content you upload.</strong> Video clips, images, and voiceover audio recordings you upload for
            editing — up to 4 GB per file. This is footage of your own life, and may include your face, your voice,
            and the faces and voices of other people who appear in it.
          </p>
          <p>
            <strong>Onboarding &amp; persona information.</strong> Your answers to our onboarding questions — what
            you do for work, where you went to school, your social handles, where you&apos;re based, your hobbies,
            your travel history, and what you&apos;re passionate about — plus any free-text conversation with our
            onboarding interview and style chat. We use this to generate a written &ldquo;creator persona&rdquo; that
            guides your content plan.
          </p>
          <p>
            <strong>Content plan &amp; feedback data.</strong> The video ideas, shot lists, and voiceover scripts
            generated for you; any notes or events you add; and feedback you give on a rendered video (thumbs
            up/down, &ldquo;more like this&rdquo;, or a free-text note).
          </p>
          <p>
            <strong>Derived data.</strong> Transcripts of your speech (produced by transcribing your uploaded audio),
            and, if you connect your TikTok, analysis of your posting patterns — see §6.
          </p>
          <p>
            <strong>Third-party account tokens.</strong> If you connect TikTok, Instagram, or YouTube to publish or
            pull analytics, we store an encrypted access token for that connection, its granted scopes, and basic
            account metadata (e.g. platform username). Tokens are encrypted at rest and used only for the actions you
            authorize.
          </p>
          <p>
            <strong>What we do not collect.</strong> We do not compute or store a biometric identifier such as a
            faceprint or voiceprint from your footage — our face-detection and background-segmentation features
            locate a face in a frame or separate a subject from the background for editing purposes only, and do not
            produce anything that could re-identify you across other footage. See §5 for what is nonetheless sent to
            third parties for processing.
          </p>
        </Section>

        {/* ── 2 ─────────────────────────────────────────────────────────── */}
        <Section n="2" title="How We Collect It">
          <ul className="list-disc space-y-1.5 pl-4">
            <li><strong>Directly from you</strong> — sign-in, the onboarding questionnaire and interview, file uploads, feedback you give on a video.</li>
            <li><strong>Automatically</strong> — transcripts and analysis are generated by processing the content you upload.</li>
            <li>
              <strong>From TikTok</strong> — only if you give us your handle, we fetch your public profile and video
              metadata (§6). If you use official TikTok sign-in to publish, TikTok provides basic profile info under
              the scopes you approve at connection time.
            </li>
          </ul>
        </Section>

        {/* ── 3 ─────────────────────────────────────────────────────────── */}
        <Section n="3" title="How We Use It">
          <p>We use the information above to:</p>
          <ul className="list-disc space-y-1.5 pl-4">
            <li>Generate your creator persona, content plan, shot lists, and voiceover scripts;</li>
            <li>Transcribe, analyze, and edit the footage you upload into finished videos;</li>
            <li>Match your content to music in our library and select on-screen text and pacing;</li>
            <li>Tune future plan items and hooks based on the feedback you give us (§1, feedback data);</li>
            <li>Publish content to platforms you&apos;ve explicitly connected, at your direction;</li>
            <li>Operate, secure, and improve the Service, including preventing abuse; and</li>
            <li>Communicate with you about your account, e.g. a waitlist confirmation email.</li>
          </ul>
          <p>We do not use your data for advertising, and we do not run behavioral analytics of any kind (§10).</p>
        </Section>

        {/* ── 4 ─────────────────────────────────────────────────────────── */}
        <Section n="4" title="Legal Basis for Processing (GDPR)" flag>
          <p>If you are in the European Economic Area or United Kingdom, we process your data on these legal bases:</p>
          <ul className="list-disc space-y-1.5 pl-4">
            <li><strong>Contract</strong> — processing your uploads, generating your plan, and rendering videos is necessary to provide the Service you asked for.</li>
            <li><strong>Consent</strong> — connecting a TikTok/Instagram/YouTube account, and analyzing your public TikTok profile, are actions you take affirmatively and can withdraw at any time.</li>
            <li><strong>Legitimate interests</strong> — securing the Service, preventing abuse, and maintaining the minimum operational logs needed to run it.</li>
          </ul>
        </Section>

        {/* ── 5 ─────────────────────────────────────────────────────────── */}
        <Section n="5" title="AI Sub-Processors & Third Parties">
          <p>
            To provide the Service, we share data with the following processors. This list is published and we
            commit to keeping it current.
          </p>
          <div className="overflow-x-auto rounded-lg border border-zinc-200">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-[#ffffff] text-[11px] uppercase tracking-[0.06em] text-[#a1a1aa]">
                <tr>
                  <th className="px-3 py-2 font-semibold">Processor</th>
                  <th className="px-3 py-2 font-semibold">What it receives</th>
                  <th className="px-3 py-2 font-semibold">Purpose</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                <tr>
                  <td className="px-3 py-2 font-medium">Google (Gemini API)</td>
                  <td className="px-3 py-2">Your raw uploaded video, transcripts, persona &amp; plan text</td>
                  <td className="px-3 py-2">Video analysis, transcription, copy generation</td>
                </tr>
                <tr>
                  <td className="px-3 py-2 font-medium">OpenAI</td>
                  <td className="px-3 py-2">Extracted audio, transcripts</td>
                  <td className="px-3 py-2">Speech-to-text, caption correction</td>
                </tr>
                <tr>
                  <td className="px-3 py-2 font-medium">Google Cloud Storage</td>
                  <td className="px-3 py-2">All uploaded media and rendered output</td>
                  <td className="px-3 py-2">File storage</td>
                </tr>
                <tr>
                  <td className="px-3 py-2 font-medium">Fly.io</td>
                  <td className="px-3 py-2">All account &amp; job data</td>
                  <td className="px-3 py-2">Application &amp; database hosting (US)</td>
                </tr>
                <tr>
                  <td className="px-3 py-2 font-medium">Vercel</td>
                  <td className="px-3 py-2">Web traffic, session cookies</td>
                  <td className="px-3 py-2">Website hosting (US)</td>
                </tr>
                <tr>
                  <td className="px-3 py-2 font-medium">Resend</td>
                  <td className="px-3 py-2">Your email address</td>
                  <td className="px-3 py-2">Transactional email (e.g. waitlist confirmation)</td>
                </tr>
                <tr>
                  <td className="px-3 py-2 font-medium">TikTok</td>
                  <td className="px-3 py-2">Publishing content, reading analytics</td>
                  <td className="px-3 py-2">Only if you connect your account</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p>
            We do not use Sentry, Google Analytics, PostHog, or any similar error-tracking or analytics vendor —
            there is none integrated into the Service. A small, currently-disabled feature may in the future send a
            sample of system-level AI outputs (not directly tied to your identity) to Anthropic for internal quality
            review; we will update this table if and when that is turned on for any user.
          </p>
        </Section>

        {/* ── 6 ─────────────────────────────────────────────────────────── */}
        <Section n="6" title="Your Public TikTok Profile">
          <p>
            If you give Kria your TikTok handle during onboarding, we fetch your <em>public</em> profile — follower
            count, and up to 30 recent videos&apos; captions, hashtags, and engagement metrics (views, likes,
            comments, shares) — the same information anyone could see by visiting your profile. We use AI to analyze
            this into a summary of your posting style, which feeds into your persona, content plan, and generated
            video hooks.
          </p>
          <p>
            This is separate from, and does not require, connecting your TikTok account via official sign-in (§5). We
            do not currently download or analyze the video files themselves from this public-profile flow. A related
            feature that would do so — downloading your TikTok videos for AI visual-style analysis — exists in our
            codebase but is switched off for all users as of this policy&apos;s effective date; if we turn it on, we
            will update this section and notify users first.
          </p>
        </Section>

        {/* ── 7 ─────────────────────────────────────────────────────────── */}
        <Section n="7" title="International Data Transfers" flag>
          <p>
            Kria&apos;s infrastructure and processors (Fly.io, Vercel, Google, OpenAI, Resend) are US-based. If you
            are located in the EEA, UK, or elsewhere outside the US, your data will be transferred to and processed
            in the United States. Where required, we rely on Standard Contractual Clauses or an equivalent legally
            recognized transfer mechanism with our processors.
          </p>
        </Section>

        {/* ── 8 ─────────────────────────────────────────────────────────── */}
        <Section n="8" title="Data Retention">
          <p>We keep data for as long as needed to provide the Service, specifically:</p>
          <ul className="list-disc space-y-1.5 pl-4">
            <li><strong>Account data</strong> (name, email) — for as long as your account is active, then deleted on request per §9.</li>
            <li><strong>Uploaded footage and rendered videos</strong> — retained until you delete them or close your account. We do not currently auto-delete finished videos or the source footage behind them, because you may want to re-edit or re-download them later.</li>
            <li><strong>Anonymous or session-only uploads</strong> (e.g. a not-yet-signed-in trial) — automatically deleted after 24 hours.</li>
            <li><strong>Voiceover recordings and generated music renders</strong> — automatically deleted after 24 hours once incorporated into your final video.</li>
            <li><strong>Speech transcripts</strong> — cached to avoid re-processing identical audio; we are moving this cache onto the same 24-hour retention window described above.</li>
            <li><strong>Internal AI processing logs</strong> tied to a specific job — deleted after 30 days.</li>
          </ul>
          <p>
            When you delete your account, we delete your account record and uploaded/rendered media from our active
            storage; residual copies in short-lived backups are purged on our normal backup rotation (no longer than
            90 days).
          </p>
        </Section>

        {/* ── 9 ─────────────────────────────────────────────────────────── */}
        <Section n="9" title="Your Rights" flag>
          <p>Depending on where you live, you have the right to:</p>
          <ul className="list-disc space-y-1.5 pl-4">
            <li><strong>Access</strong> a copy of the personal data we hold about you;</li>
            <li><strong>Correct</strong> inaccurate data;</li>
            <li><strong>Delete</strong> your account and associated data (&ldquo;right to be forgotten&rdquo;);</li>
            <li><strong>Export</strong> your data in a portable format;</li>
            <li><strong>Restrict or object</strong> to certain processing; and</li>
            <li><strong>Withdraw consent</strong> for anything based on consent (e.g. TikTok analysis), at any time.</li>
          </ul>
          <p>
            <strong>California residents (CCPA/CPRA):</strong> you have the right to know what personal information
            we collect, request its deletion, correct it, and opt out of its &ldquo;sale&rdquo; or &ldquo;sharing.&rdquo;{" "}
            <strong>We do not sell or share your personal information</strong>, and we will not discriminate against
            you for exercising any of these rights.
          </p>
          <p>
            To exercise any of these rights, email{" "}
            <a href={`mailto:${PRIVACY_EMAIL}`} className="text-lime-700 underline underline-offset-2">
              {PRIVACY_EMAIL}
            </a>
            . We will respond within {REQUEST_RESPONSE_DAYS} days. We may ask you to verify your identity before
            fulfilling a request.
          </p>
        </Section>

        {/* ── 10 ─────────────────────────────────────────────────────────── */}
        <Section n="10" title="Cookies & Tracking" flag>
          <p>
            Kria uses only the cookies required to keep you signed in — session, CSRF-protection, and sign-in-flow
            cookies set by our authentication provider. We do not use analytics cookies, advertising cookies, or any
            third-party tracking pixels. Because we only use strictly necessary cookies, we do not display a cookie
            consent banner.
          </p>
          <p>
            <strong>We run no analytics platform of any kind</strong> — no Google Analytics, no product analytics
            tool, no session-replay tool, and no advertising network. Your IP address is read transiently to rate-limit
            abuse on certain endpoints and is not stored in our database.
          </p>
        </Section>

        {/* ── 11 ─────────────────────────────────────────────────────────── */}
        <Section n="11" title="Security">
          <p>
            We use encryption in transit (HTTPS/TLS) across the Service, and encrypt sensitive credentials — such as
            connected third-party account tokens — at rest. Access to production data is restricted. No system is
            100% secure, and we cannot guarantee absolute security of information transmitted to or from the Service.
          </p>
        </Section>

        {/* ── 12 ─────────────────────────────────────────────────────────── */}
        <Section n="12" title="Children's Privacy">
          <p>
            Kria is intended for users 18 and older. We do not knowingly collect personal data from anyone under 18.
            If you believe a minor has provided us data, contact{" "}
            <a href={`mailto:${PRIVACY_EMAIL}`} className="text-lime-700 underline underline-offset-2">
              {PRIVACY_EMAIL}
            </a>{" "}
            and we will delete it.
          </p>
        </Section>

        {/* ── 13 ─────────────────────────────────────────────────────────── */}
        <Section n="13" title="Contact & Complaints">
          <p>
            Questions, requests, or concerns about this policy: {LEGAL_ENTITY},{" "}
            <a href={`mailto:${PRIVACY_EMAIL}`} className="text-lime-700 underline underline-offset-2">
              {PRIVACY_EMAIL}
            </a>
            , {LEGAL_ADDRESS}.
          </p>
          <p>
            If you are in the EEA or UK and believe we have not addressed your concern, you have the right to lodge a
            complaint with your local data protection authority. This policy is governed by the laws of{" "}
            {GOVERNING_LAW}, without prejudice to any mandatory data-protection rights available to you locally.
          </p>
        </Section>

        {/* ── 14 ─────────────────────────────────────────────────────────── */}
        <Section n="14" title="Changes to This Policy">
          <p>
            We may update this policy as the Service changes. For material changes — such as adding a new
            sub-processor, a new category of data, or a new use of your data — we will notify you by email or an
            in-app notice before the change takes effect, and update the &ldquo;Effective&rdquo; date above.
          </p>
        </Section>

        <p className="mt-10 text-[12px] leading-relaxed text-[#a1a1aa]">
          This document was drafted with AI assistance for planning purposes and does not constitute legal advice. It
          must be reviewed by a qualified attorney specializing in data privacy law before publication — see the
          compliance checklist in docs/legal/README.md.
        </p>
      </div>
    </main>
  );
}
