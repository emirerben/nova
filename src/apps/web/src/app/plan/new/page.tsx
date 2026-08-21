"use client";

/**
 * /plan/new — the New-video flow's full-screen steps.
 *
 * Step 1 "What kind of video?" for every type; montage adds Step 2 "Pick a
 * style." (Classic / Masonry / Polaroid — design: Paper "V2 — Item setup per
 * type", board S2) so the template choice is never skipped. Reuses the item
 * page's SetupPicker cards/data so type + style vocabulary live in one place.
 *
 * The plan item is created only on the FINAL Continue (abandon leaves
 * nothing): addIdea → updatePlanItem(edit_format [+ montage_preset]) → item
 * page with ?setup=done so the setup receipt leads and the uploader is first.
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useSession } from "next-auth/react";
import SignInPrompt from "@/app/plan/_components/SignInPrompt";
import { LightShell } from "@/components/ui/LightShell";
import {
  addIdea,
  getContentPlan,
  updatePlanItem,
  type ContentPlan,
  type MontagePreset,
} from "@/lib/plan-api";
import type { PickerEditFormat } from "@/lib/edit-format";
import {
  MediaRadioCard,
  persistedEditFormatFor,
  STYLE_TILES,
  TYPE_COPY,
  TYPE_MEDIA,
} from "@/app/plan/items/[id]/components/SetupPicker";

const _subtitledRaw = (process.env.NEXT_PUBLIC_SUBTITLED_ENABLED ?? "").trim();
const SUBTITLED_ENABLED = _subtitledRaw.toLowerCase() === "true" || _subtitledRaw === "1";

/** WAI radio-group arrows (same pattern as SetupPicker's rail). */
function radioGroupKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
  const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"];
  if (!keys.includes(event.key)) return;
  const radios = Array.from(
    event.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]'),
  );
  const current = radios.indexOf(document.activeElement as HTMLElement);
  if (current === -1 || radios.length === 0) return;
  event.preventDefault();
  const delta = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
  radios[(current + delta + radios.length) % radios.length]?.focus();
}

export default function NewVideoPage() {
  const { status: authStatus } = useSession();
  const router = useRouter();

  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [planState, setPlanState] = useState<"loading" | "ready" | "missing">("loading");
  const [step, setStep] = useState<"kind" | "style">("kind");
  const [selected, setSelected] = useState<PickerEditFormat>("montage");
  const [selectedStyle, setSelectedStyle] = useState<MontagePreset>("classic");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (authStatus !== "authenticated") return;
    let cancelled = false;
    getContentPlan()
      .then((p) => {
        if (cancelled) return;
        if (p) {
          setPlan(p);
          setPlanState("ready");
        } else {
          setPlanState("missing");
        }
      })
      .catch(() => {
        if (!cancelled) setPlanState("missing");
      });
    return () => {
      cancelled = true;
    };
  }, [authStatus]);

  // No plan yet (brand-new user) → the /plan router owns onboarding.
  useEffect(() => {
    if (planState === "missing") router.replace("/plan");
  }, [planState, router]);

  const typeValues: PickerEditFormat[] = [
    "montage",
    "narrated_planned",
    ...(SUBTITLED_ENABLED ? (["subtitled"] as PickerEditFormat[]) : []),
  ];

  // Montage is the only type with a style step: kind (1/3) → style (2/3) →
  // footage on the item page (3/3). Other types go kind (1/2) → footage (2/2).
  const isMontage = selected === "montage";
  const totalSteps = isMontage ? 3 : 2;

  const createItem = useCallback(async () => {
    if (creating || !plan) return;
    setCreating(true);
    setError(null);
    let itemId: string;
    try {
      const item = await addIdea(plan.id, TYPE_COPY[selected].label);
      itemId = item.id;
    } catch {
      setError("That didn't go through — try again.");
      setCreating(false);
      return;
    }
    try {
      await updatePlanItem(itemId, {
        edit_format: persistedEditFormatFor(selected),
        ...(selected === "montage"
          ? { content_mode: "existing_footage" as const, montage_preset: selectedStyle }
          : {}),
      });
      router.push(`/plan/items/${itemId}?setup=done`);
    } catch {
      // Item exists but the type didn't stick — land on the item page with the
      // TYPE rail open so the user can re-pick there. No dead end.
      router.push(`/plan/items/${itemId}`);
    }
  }, [creating, plan, selected, selectedStyle, router]);

  const onContinue = useCallback(() => {
    if (step === "kind" && selected === "montage") {
      setStep("style");
      return;
    }
    void createItem();
  }, [step, selected, createItem]);

  if (authStatus === "loading") {
    return <LightShell size="narrow">{null}</LightShell>;
  }
  if (authStatus !== "authenticated") {
    return (
      <LightShell size="narrow">
        <SignInPrompt
          callbackUrl="/plan/new"
          title="Sign in to make a video"
          subtitle="Pick what kind, add your footage — Kria edits it into a post."
        />
      </LightShell>
    );
  }

  const onStyleStep = step === "style";

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto flex min-h-screen max-w-[900px] flex-col px-6 pt-6">
        <div className="flex items-center justify-between">
          {onStyleStep ? (
            <button
              type="button"
              onClick={() => setStep("kind")}
              aria-label="Back to video kind"
              className="flex h-11 w-11 items-center justify-center rounded-full text-[20px] leading-none text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
            >
              ‹
            </button>
          ) : (
            <Link
              href="/plan"
              aria-label="Back to your videos"
              className="flex h-11 w-11 items-center justify-center rounded-full text-[22px] leading-none text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
            >
              ×
            </Link>
          )}
          <span className="text-[12px] text-[#71717a]">
            {onStyleStep ? `Step 2 of ${totalSteps}` : `Step 1 of ${totalSteps}`}
          </span>
          <span aria-hidden="true" className="h-11 w-11" />
        </div>

        {onStyleStep ? (
          <>
            <h1 className="font-display mt-6 text-[30px] font-medium leading-tight text-[#0c0c0e]">
              Pick a style.
            </h1>
            <p className="mt-1.5 text-sm text-[#71717a]">How your clips are arranged.</p>

            <div
              className="mt-6 grid grid-cols-2 gap-3.5 pb-4 sm:grid-cols-3"
              role="radiogroup"
              aria-label="Montage style"
              onKeyDown={radioGroupKeyDown}
            >
              {STYLE_TILES.map((tile) => (
                <MediaRadioCard
                  key={tile.value}
                  active={selectedStyle === tile.value}
                  saving={creating}
                  poster={tile.poster}
                  video={tile.video}
                  scrim="h-1/2"
                  label={tile.label}
                  desc={tile.desc}
                  onSelect={() => setSelectedStyle(tile.value)}
                />
              ))}
            </div>
          </>
        ) : (
          <>
            <h1 className="font-display mt-6 text-[30px] font-medium leading-tight text-[#0c0c0e]">
              What kind of video?
            </h1>
            <p className="mt-1.5 text-sm text-[#71717a]">Kria edits each kind differently.</p>

            <div
              className="-mx-6 mt-6 flex snap-x snap-mandatory gap-3.5 overflow-x-auto px-6 py-1 [scroll-padding-inline:1.5rem] sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:p-0 lg:grid-cols-3"
              role="radiogroup"
              aria-label="What kind of video"
              onKeyDown={radioGroupKeyDown}
            >
              {typeValues.map((value) => (
                <MediaRadioCard
                  key={value}
                  active={selected === value}
                  saving={creating}
                  poster={TYPE_MEDIA[value].poster}
                  video={TYPE_MEDIA[value].video}
                  scrim="h-3/5"
                  label={TYPE_COPY[value].label}
                  desc={TYPE_COPY[value].desc}
                  meta={TYPE_COPY[value].meta}
                  onSelect={() => setSelected(value)}
                />
              ))}
            </div>
          </>
        )}

        {error && (
          <p role="alert" className="mt-4 text-sm text-[#3f3f46]">
            {error}
          </p>
        )}

        <div className="sticky bottom-0 z-10 -mx-6 mt-auto border-t border-zinc-200 bg-white px-6 pb-[max(16px,env(safe-area-inset-bottom))] pt-4">
          <button
            type="button"
            onClick={onContinue}
            disabled={creating || planState !== "ready"}
            className="min-h-12 w-full rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white hover:opacity-80 disabled:bg-zinc-700"
          >
            {creating ? "Setting up…" : "Continue"}
          </button>
          <p className="mt-2 text-center text-[12px] text-[#71717a]">
            {onStyleStep || !isMontage ? "Next: add your footage" : "Next: pick a style"}
          </p>
        </div>
      </div>
    </div>
  );
}
