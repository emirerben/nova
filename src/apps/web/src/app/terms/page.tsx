/**
 * /terms — static Terms of Service.
 *
 * Public route (middleware.ts matcher only covers /admin/*), no session read,
 * statically prerendered. Drafted against the terms-of-service skill's
 * 16-section template, tailored to what Kria actually does — see
 * /Users/emirerben/.claude/plans/run-npx-skills-use-floating-church.md (A1)
 * for the sourcing behind each clause.
 *
 * DISCLAIMER: generated for planning purposes, not legal advice. Every
 * [⚠️ …] marker below needs a real answer and/or attorney review before
 * this page is safe to link from production. See docs/legal/README.md.
 */
import Link from "next/link";
import type { ReactNode } from "react";

import { Eyebrow } from "@/components/ui/Eyebrow";
import {
  CHANGE_NOTICE_DAYS,
  EFFECTIVE_DATE,
  GOVERNING_LAW,
  LEGAL_ADDRESS,
  LEGAL_EMAIL,
  LEGAL_ENTITY,
  LIABILITY_CAP_USD,
} from "@/lib/legal";

export const metadata = {
  title: "Terms — Kria",
};

function Section({
  n,
  title,
  summary,
  children,
}: {
  n: string;
  title: string;
  summary: string;
  children: ReactNode;
}) {
  return (
    <section id={`s${n}`} className="scroll-mt-20 border-t border-zinc-100 py-9 first:border-t-0 first:pt-0">
      <Eyebrow tone="muted" className="mb-2">
        {n}. {title}
      </Eyebrow>
      <p className="mb-4 text-[13px] italic leading-relaxed text-[#71717a]">{summary}</p>
      <div className="space-y-3 text-[15px] leading-relaxed text-[#3f3f46]">{children}</div>
    </section>
  );
}

const TOC: { n: string; title: string }[] = [
  { n: "1", title: "Agreement to These Terms" },
  { n: "2", title: "Description of the Service" },
  { n: "3", title: "Account Registration & Security" },
  { n: "4", title: "Acceptable Use" },
  { n: "5", title: "Your Content" },
  { n: "6", title: "AI Processing of Your Content" },
  { n: "7", title: "Ownership of Rendered Output" },
  { n: "8", title: "Music & Licensed Assets" },
  { n: "9", title: "Third-Party Platforms" },
  { n: "10", title: "Free Service, Pricing & Future Plans" },
  { n: "11", title: "Service Availability" },
  { n: "12", title: "Intellectual Property" },
  { n: "13", title: "Limitation of Liability" },
  { n: "14", title: "Indemnification" },
  { n: "15", title: "Termination" },
  { n: "16", title: "Dispute Resolution" },
  { n: "17", title: "Changes to These Terms" },
  { n: "18", title: "Miscellaneous" },
];

export default function TermsPage() {
  return (
    <main className="min-h-screen bg-[#ffffff] text-[#0c0c0e]">
      <div className="mx-auto max-w-[680px] px-6 pb-24 pt-16">
        <Eyebrow tone="lime" className="mb-3">
          Legal
        </Eyebrow>
        <h1 className="font-display mb-2 text-[36px] font-medium leading-snug">Terms of Service</h1>
        <p className="mb-8 text-[13px] text-[#a1a1aa]">
          Effective {EFFECTIVE_DATE}. See also our{" "}
          <Link href="/privacy" className="text-lime-700 underline underline-offset-2">
            Privacy Policy
          </Link>
          , which these Terms incorporate by reference.
        </p>

        {/* ── PLAIN-LANGUAGE SUMMARY ───────────────────────────────────── */}
        <div className="mb-10 rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
          <Eyebrow tone="muted" className="mb-3">
            Key terms, in plain English
          </Eyebrow>
          <ul className="list-disc space-y-1.5 pl-4 text-[14px] leading-relaxed text-[#3f3f46]">
            <li>Your footage and your rendered videos are yours. We never sell them or use them to train our own models.</li>
            <li>Your raw video and audio are sent to Google and OpenAI to power the editing — see §6.</li>
            <li>You&apos;re responsible for having the right to film and post everyone in your footage.</li>
            <li>Kria is free right now. That could change — we&apos;ll give you {CHANGE_NOTICE_DAYS} days&apos; notice first.</li>
            <li>
              <strong>Kria does not hold or warrant any license for the music in its library</strong> — using a track,
              and posting a video that includes it, is entirely at your own risk. See §8.
            </li>
            <li>
              <strong>Kria is currently operated by an individual, not a company</strong> — see §12 and §13 for what that
              means for liability.
            </li>
          </ul>
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

        {/* ── 1 ─────────────────────────────────────────────────────────── */}
        <Section n="1" title="Agreement to These Terms" summary="How you accept these Terms, and who can use Kria.">
          <p>
            These Terms of Service (&ldquo;<strong>Terms</strong>&rdquo;) are a binding agreement between you
            (&ldquo;<strong>you</strong>&rdquo;) and {LEGAL_ENTITY}, doing business as Kria (&ldquo;
            <strong>Kria</strong>&rdquo;, &ldquo;<strong>we</strong>&rdquo;, &ldquo;<strong>us</strong>&rdquo;), governing
            your access to and use of the Kria website, application, and related services (the
            &ldquo;<strong>Service</strong>&rdquo;).
          </p>
          <p>
            By checking the box or clicking &ldquo;Continue&rdquo; when you sign in, you affirmatively agree to these
            Terms and our Privacy Policy. If you do not agree, do not use the Service.
          </p>
          <p>
            You must be at least 18 years old to use Kria. We do not knowingly permit anyone under 18 to create an
            account, and we have no age-verification mechanism beyond this statement — if we learn an account belongs
            to someone under 18, we will close it.
          </p>
          <p>
            If you use Kria on behalf of a business or other organization, you represent that you have the authority
            to bind that organization to these Terms, and &ldquo;you&rdquo; refers to that organization as well as
            you individually.
          </p>
        </Section>

        {/* ── 2 ─────────────────────────────────────────────────────────── */}
        <Section n="2" title="Description of the Service" summary="What Kria does, and what it doesn't promise.">
          <p>
            Kria is an AI content agent for short-form video creators. Based on a short interview and, optionally,
            your public TikTok profile, it builds you a content plan, tells you what to film, and edits the footage
            you upload into finished vertical videos — with music, pacing, and text overlays.
          </p>
          <p>
            The Service is <strong>best-effort and AI-assisted</strong>. We do not guarantee that any particular
            edit, song match, or overlay will be generated, that generated content will be accurate, appropriate, or
            free of errors, or that the Service will meet your expectations. Some features (for example, matching
            your footage to a licensed song) may be skipped entirely if no suitable option is found — the Service is
            designed to degrade gracefully rather than fail outright, but that means output quality and completeness
            can vary.
          </p>
          <p>
            Output is bounded by the footage you supply. Kria edits and arranges what you film; it cannot add footage
            you didn&apos;t capture.
          </p>
        </Section>

        {/* ── 3 ─────────────────────────────────────────────────────────── */}
        <Section n="3" title="Account Registration & Security" summary="How you sign in, and what you're responsible for.">
          <p>
            You create a Kria account by signing in with Google. We receive your name and email address from Google
            at sign-in; we do not receive or store your Google password.
          </p>
          <p>
            You are responsible for maintaining the security of the Google account used to sign in, and for all
            activity that occurs under your Kria account. Notify us immediately at{" "}
            <a href={`mailto:${LEGAL_EMAIL}`} className="text-lime-700 underline underline-offset-2">
              {LEGAL_EMAIL}
            </a>{" "}
            if you suspect unauthorized use.
          </p>
          <p>Accounts are for a single individual creator. Account sharing across multiple people is not permitted.</p>
        </Section>

        {/* ── 4 ─────────────────────────────────────────────────────────── */}
        <Section n="4" title="Acceptable Use" summary="What you can't do with Kria.">
          <p>You agree not to:</p>
          <ul className="list-disc space-y-1.5 pl-4">
            <li>Upload footage of any identifiable person without that person&apos;s knowledge and consent to be filmed and shown in content you may publish;</li>
            <li>Upload or generate content depicting minors in a sexualized, exploitative, or otherwise unlawful manner;</li>
            <li>Use Kria to create deepfakes, impersonate a real person, or misrepresent AI-generated content as unedited footage of events that didn&apos;t occur;</li>
            <li>Upload content you don&apos;t have the rights to, or that infringes anyone&apos;s copyright, trademark, privacy, or publicity rights;</li>
            <li>Use the Service for anything illegal, or to harass, defame, or threaten any person;</li>
            <li>Scrape, reverse engineer, decompile, or attempt to extract the models, prompts, or source code underlying the Service;</li>
            <li>Interfere with or overload our infrastructure, or attempt to access another user&apos;s account or content;</li>
            <li>Resell, sublicense, or offer the Service (or substantially similar functionality built by observing it) as a competing product.</li>
          </ul>
          <p>We may suspend or terminate accounts that violate this section, with or without notice, as described in §15.</p>
        </Section>

        {/* ── 5 ─────────────────────────────────────────────────────────── */}
        <Section n="5" title="Your Content" summary="You own what you upload. You're responsible for having the right to upload it — including everyone else in the shot.">
          <p>
            &ldquo;<strong>Your Content</strong>&rdquo; means the video, audio, images, and text you upload or type
            into Kria, including footage, voiceover recordings, and answers to the onboarding questionnaire.
          </p>
          <p>
            <strong>You own Your Content.</strong> We claim no ownership over it. By uploading it, you grant us a
            limited, non-exclusive, worldwide license to host, store, process, transmit, and modify Your Content
            solely to operate, maintain, and improve the Service for you — including sending it to the third-party AI
            processors described in §6. This license ends when Your Content is deleted, except for copies retained
            briefly in backups, which are purged on our normal backup rotation.
          </p>
          <p>
            <strong>You are solely responsible for Your Content</strong>, including for having all rights necessary
            to upload it and to have it appear in the videos Kria produces. This specifically includes obtaining the
            consent of every identifiable person who appears in your footage — Kria has no way to verify this and
            does not pre-screen uploads. If a third party claims that Your Content infringes their rights or was
            filmed without their consent, you — not Kria — are responsible for resolving that claim (see §14,
            Indemnification).
          </p>
          <p>
            We do not pre-screen Your Content before you upload it, but we may remove content or suspend accounts
            that we become aware violate §4 or applicable law.
          </p>
        </Section>

        {/* ── 6 ─────────────────────────────────────────────────────────── */}
        <Section
          n="6"
          title="AI Processing of Your Content"
          summary="Your raw video and audio leave our servers to be processed by Google and OpenAI. We don't train our own models on it."
        >
          <p>
            To generate transcripts, analysis, and edits, Kria transmits Your Content — including full, unedited
            video files and extracted audio — to third-party AI providers, principally Google (Gemini) and OpenAI
            (Whisper transcription, caption correction). These providers process Your Content under their own terms
            and, where applicable, data processing agreements; our full sub-processor list and what each one receives
            is published in our{" "}
            <Link href="/privacy" className="text-lime-700 underline underline-offset-2">
              Privacy Policy
            </Link>
            .
          </p>
          <p>
            <strong>We do not use Your Content to train our own foundation models</strong>, and we instruct our
            processors not to use it to train theirs outside of the terms they separately publish for API customers.
            A sampled fraction of de-identified system prompts and outputs may be sent to a secondary AI provider
            (Anthropic) for internal quality evaluation; this is off by default and, when enabled, is not used to
            identify you.
          </p>
        </Section>

        {/* ── 7 ─────────────────────────────────────────────────────────── */}
        <Section n="7" title="Ownership of Rendered Output" summary="The finished videos Kria produces for you are yours to use.">
          <p>
            Subject to §8 (music licensing) and any pre-existing rights in Your Content, you own the videos Kria
            renders for you and may use them however you like, including posting them commercially.
          </p>
          <p>
            <strong>Note on AI-generated material:</strong> copyright law in some jurisdictions, including current
            U.S. Copyright Office guidance, treats material generated substantially by AI (as opposed to selected,
            arranged, and directed by a human) as ineligible for copyright protection on its own. We grant you all
            rights we hold in the rendered output; we cannot grant rights that don&apos;t legally exist. Your original
            footage remains protected by copyright as your own creative work regardless of this limitation.
          </p>
        </Section>

        {/* ── 8 ─────────────────────────────────────────────────────────── */}
        <Section
          n="8"
          title="Music & Licensed Assets"
          summary="Kria does not hold or warrant any license for the music in its library. Choosing to use a track — and posting a video that includes it — is entirely your decision and at your own risk."
        >
          <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-[14px] text-amber-900">
            <strong>Kria does not hold, and does not warrant that it holds, any copyright license, synchronization
            license, or other clearance for any track in its music library.</strong> Tracks are offered as a
            convenience for matching and pacing your footage — not as licensed, cleared audio. We grant you no
            license to any track, and we make no representation that using a track is lawful in your jurisdiction or
            permitted on any platform.
          </p>
          <p>
            By choosing to use a track from the library, you are independently deciding — at your own risk — whether
            you have the right to do so. This may depend on the platform&apos;s own music-licensing arrangements (for
            example, some platforms license certain catalogs directly for in-app posting, which may or may not cover
            audio added through a third-party tool like Kria), the specific track and rightsholder, and your
            jurisdiction. Kria does not verify or warrant any of this on your behalf.
          </p>
          <p>
            You are solely responsible for any copyright claim, content-ID match, takedown, platform strike, or legal
            claim arising from a track&apos;s use in a video you post, and you agree to indemnify Kria for any such
            claim under §14. Kria is not liable for lost reach, demonetization, account action, or any other
            consequence of using a track from the library.
          </p>
        </Section>

        {/* ── 9 ─────────────────────────────────────────────────────────── */}
        <Section n="9" title="Third-Party Platforms" summary="If you connect or publish to TikTok, Instagram, or YouTube, their terms govern that part.">
          <p>
            Kria may let you connect third-party accounts (currently TikTok, with Instagram and YouTube support) to
            publish directly or pull analytics. Doing so is entirely optional and at your direction. Your use of
            those platforms is governed by their own terms of service, community guidelines, and privacy policies,
            not ours. We are not responsible for actions those platforms take on your account — including removing
            content, restricting reach, or suspending your account — even if the content originated from Kria.
          </p>
          <p>
            You can disconnect a connected account at any time from your account settings; doing so revokes our
            access to it going forward.
          </p>
        </Section>

        {/* ── 10 ─────────────────────────────────────────────────────────── */}
        <Section n="10" title="Free Service, Pricing & Future Plans" summary="Kria is free today. We may introduce paid plans later, with notice.">
          <p>
            Kria is currently provided free of charge, and no part of the Service requires payment. We may introduce
            paid plans, usage limits, or discontinue free access to some or all features in the future. If we do, we
            will give you at least {CHANGE_NOTICE_DAYS} days&apos; notice before any change that affects your
            existing account, and these Terms will be updated with the applicable billing terms (subscription
            structure, billing cycle, refund policy, and cancellation process) before any charge is made.
          </p>
        </Section>

        {/* ── 11 ─────────────────────────────────────────────────────────── */}
        <Section n="11" title="Service Availability" summary="We don't currently promise uptime.">
          <p>
            We aim to keep Kria available and reliable, but we do not guarantee uninterrupted access. The Service may
            be unavailable during maintenance, due to third-party outages (including the AI providers in §6), or for
            reasons beyond our reasonable control (force majeure), including but not limited to acts of God, internet
            or utility failures, and government action. We currently offer no service-level agreement or uptime
            commitment.
          </p>
        </Section>

        {/* ── 12 ─────────────────────────────────────────────────────────── */}
        <Section n="12" title="Intellectual Property" summary="We own the Kria platform, brand, and technology. You get a limited license to use it.">
          <p>
            Kria, the Kria name and logo, and the underlying software, models, prompts, and technology that power the
            Service are owned by {LEGAL_ENTITY} and are protected by intellectual property law. Subject to these
            Terms, we grant you a limited, non-exclusive, non-transferable, revocable license to access and use the
            Service for your own content creation. This license does not include any right to resell, sublicense, or
            create derivative works of the Service itself.
          </p>
        </Section>

        {/* ── 13 ─────────────────────────────────────────────────────────── */}
        <Section
          n="13"
          title="Limitation of Liability"
          summary={`Our liability to you is capped at $${LIABILITY_CAP_USD}, since the Service is currently free.`}
        >
          <p className="rounded-lg border border-zinc-200 bg-white p-3 text-[14px]">
            <strong>
              To the maximum extent permitted by law, Kria and its operator will not be liable for any indirect,
              incidental, special, consequential, or punitive damages, or for lost profits, lost data, or loss of
              goodwill, arising from your use of the Service. Our total liability to you for any claim arising from
              these Terms or the Service is limited to the greater of (a) the fees you paid us in the 12 months
              before the claim arose, or (b) ${LIABILITY_CAP_USD} USD.
            </strong>
          </p>
          <p>
            The Service is provided &ldquo;as is&rdquo; and &ldquo;as available,&rdquo; without warranties of any
            kind, express or implied, including merchantability, fitness for a particular purpose, and
            non-infringement, except where such disclaimers are not permitted by law.
          </p>
          <p>
            <strong>What this cap does not limit:</strong> nothing in these Terms excludes or limits liability for
            death or personal injury caused by negligence, fraud or fraudulent misrepresentation, or any other
            liability that cannot lawfully be excluded or limited. If you are a consumer in the European Union or
            United Kingdom, mandatory consumer-protection law in your jurisdiction may grant you rights this section
            cannot override, and those rights control where they conflict with the above.
          </p>
        </Section>

        {/* ── 14 ─────────────────────────────────────────────────────────── */}
        <Section n="14" title="Indemnification" summary="You cover claims that arise from your content or your misuse of the Service.">
          <p>
            You agree to indemnify and hold harmless {LEGAL_ENTITY} from any claim, liability, damages, or expense
            (including reasonable legal fees) arising from: (a) Your Content, including any claim that it infringes
            a third party&apos;s rights or was created or posted without a depicted person&apos;s consent; (b) your
            violation of §4 (Acceptable Use); or (c) your violation of any law or third party&apos;s rights in
            connection with your use of the Service.
          </p>
        </Section>

        {/* ── 15 ─────────────────────────────────────────────────────────── */}
        <Section n="15" title="Termination" summary="You can leave anytime. We can suspend accounts that violate these Terms.">
          <p>
            You may stop using Kria and request deletion of your account at any time (see our{" "}
            <Link href="/privacy" className="text-lime-700 underline underline-offset-2">
              Privacy Policy
            </Link>{" "}
            for how). We may suspend or terminate your access if you materially violate these Terms, including §4,
            or if required by law.
          </p>
          <p>
            On termination, your right to use the Service ends immediately. Sections 5–7 (content and output
            ownership, as they apply to content already delivered to you), 12–14 (IP, liability, indemnification),
            and 16–18 (dispute resolution and miscellaneous) survive termination.
          </p>
        </Section>

        {/* ── 16 ─────────────────────────────────────────────────────────── */}
        <Section n="16" title="Dispute Resolution" summary="Governed by the law named below. No mandatory arbitration or class-action waiver.">
          <p>
            These Terms are governed by the laws of {GOVERNING_LAW}, without regard to its conflict-of-laws
            principles. We do not currently require mandatory arbitration or a class-action waiver as a condition of
            using the Service. If you are a consumer, nothing here limits any right you have to bring a claim in your
            local courts or file a complaint with a consumer protection or data protection authority in your country
            of residence.
          </p>
        </Section>

        {/* ── 17 ─────────────────────────────────────────────────────────── */}
        <Section n="17" title="Changes to These Terms" summary={`We'll give you ${CHANGE_NOTICE_DAYS} days' notice before a material change takes effect.`}>
          <p>
            We may update these Terms from time to time. For material changes, we will provide at least{" "}
            {CHANGE_NOTICE_DAYS} days&apos; notice by email or an in-app notice before the change takes effect, and
            update the &ldquo;Effective&rdquo; date at the top of this page. If you continue using the Service after
            a change takes effect, that constitutes acceptance; if you disagree with a change, you may stop using the
            Service and request account deletion before it takes effect.
          </p>
        </Section>

        {/* ── 18 ─────────────────────────────────────────────────────────── */}
        <Section n="18" title="Miscellaneous" summary="The standard boilerplate: severability, entire agreement, assignment, notices.">
          <p>
            If any provision of these Terms is found unenforceable, the remaining provisions remain in full effect.
            These Terms, together with our Privacy Policy, constitute the entire agreement between you and Kria
            regarding the Service. We may assign these Terms in connection with a merger, acquisition, or sale of
            assets; you may not assign your rights under these Terms without our consent. Our failure to enforce any
            provision is not a waiver of our right to do so later.
          </p>
          <p>
            Questions about these Terms:{" "}
            <a href={`mailto:${LEGAL_EMAIL}`} className="text-lime-700 underline underline-offset-2">
              {LEGAL_EMAIL}
            </a>
            . {LEGAL_ENTITY}, {LEGAL_ADDRESS}.
          </p>
        </Section>

        <p className="mt-10 text-[12px] leading-relaxed text-[#a1a1aa]">
          This document was drafted with AI assistance for planning purposes and does not constitute legal advice. It
          must be reviewed by a qualified attorney before publication — see the compliance checklist in
          docs/legal/README.md.
        </p>
      </div>
    </main>
  );
}
