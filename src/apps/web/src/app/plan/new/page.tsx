"use client";

/**
 * /plan/new — the New-video flow's full-screen steps.
 *
 * Step 1 "What kind of video?" for every type; montage adds Step 2 "Pick a
 * style." (Classic / Masonry / Polaroid — design: Paper "V2 — Item setup per
 * type", board S2) so the template choice is never skipped. Reuses the item
 * page's SetupPicker cards/data so type + style vocabulary live in one place.
 *
 * Tap-to-advance: selecting a kind card either advances to the style step
 * (montage) or creates the item immediately (everything else); selecting a
 * style card creates the item immediately. There is no Continue button —
 * the plan item is created the moment a final choice is made (abandon before
 * that leaves nothing): addIdea → updatePlanItem(edit_format [+
 * montage_preset]) → item page with ?setup=done so the setup receipt leads
 * and the uploader is first.
 *
 * Lane J — `?item=<id>` (+ optional `&step=kind|style`) repurposes this same
 * chooser as the item-setup page's "Back" destination: no item is created,
 * the existing item's kind/style is pre-selected from the server, and a tap
 * PATCHes it via updatePlanItem instead of addIdea, then returns to the item
 * page. The × / back control cancels to the item page instead of /plan.
 */

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { ChevronLeft } from "lucide-react";
import { useSession } from "next-auth/react";
import SignInPrompt from "@/app/plan/_components/SignInPrompt";
import { LightShell } from "@/components/ui/LightShell";
import { Button } from "@/components/ui/button";
import {
  addIdea,
  getContentPlan,
  getPlanItem,
  updatePlanItem,
  type ContentPlan,
  type MontagePreset,
} from "@/lib/plan-api";
import { resolvePickerFormat, type PickerEditFormat } from "@/lib/edit-format";
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
  return (
    <Suspense>
      <NewVideoPageInner />
    </Suspense>
  );
}

function NewVideoPageInner() {
  const { status: authStatus } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();

  // Lane J: editing an existing item's kind/style rather than creating a new
  // one. itemId is read once at mount — the URL doesn't change within this
  // page's lifetime (a submit navigates away entirely).
  const [itemId] = useState<string | null>(() => searchParams.get("item"));
  const [step, setStep] = useState<"kind" | "style">(() =>
    itemId && searchParams.get("step") === "style" ? "style" : "kind",
  );

  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [planState, setPlanState] = useState<"loading" | "ready" | "missing">(
    itemId ? "ready" : "loading",
  );
  const [itemLoadState, setItemLoadState] = useState<"loading" | "ready" | "missing">(
    itemId ? "loading" : "ready",
  );
  const [selected, setSelected] = useState<PickerEditFormat>("montage");
  const [selectedStyle, setSelectedStyle] = useState<MontagePreset>("classic");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // New-item mode: fetch the plan so a final tap can addIdea(plan.id, ...).
  useEffect(() => {
    if (authStatus !== "authenticated" || itemId) return;
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
  }, [authStatus, itemId]);

  // Edit-existing-item mode: pre-select the item's current kind/style from
  // the server instead of defaulting to montage/classic.
  useEffect(() => {
    if (authStatus !== "authenticated" || !itemId) return;
    let cancelled = false;
    getPlanItem(itemId)
      .then((it) => {
        if (cancelled) return;
        setSelected(resolvePickerFormat(it.edit_format, SUBTITLED_ENABLED));
        setSelectedStyle((it.montage_preset as MontagePreset | null) ?? "classic");
        setItemLoadState("ready");
      })
      .catch(() => {
        if (!cancelled) setItemLoadState("missing");
      });
    return () => {
      cancelled = true;
    };
  }, [authStatus, itemId]);

  // No plan yet (brand-new user) → the /plan router owns onboarding. Only
  // applies in new-item mode — edit-existing-item mode never touches the plan.
  useEffect(() => {
    if (planState === "missing") router.replace("/plan");
  }, [planState, router]);

  // Item vanished (deleted / bad id) mid edit-flow → back to the plan list.
  useEffect(() => {
    if (itemLoadState === "missing") router.replace("/plan");
  }, [itemLoadState, router]);

  const typeValues: PickerEditFormat[] = [
    "montage",
    "narrated_planned",
    ...(SUBTITLED_ENABLED ? (["subtitled"] as PickerEditFormat[]) : []),
  ];

  // Montage is the only type with a style step: kind (1/3) → style (2/3) →
  // footage on the item page (3/3). Other types go kind (1/2) → footage (2/2).
  const isMontage = selected === "montage";
  const totalSteps = isMontage ? 3 : 2;

  // Takes explicit args rather than reading `selected`/`selectedStyle` state:
  // the caller just called setSelected/setSelectedStyle, and that state read
  // would still be stale on this render (state updates aren't synchronous).
  //
  // Edit-existing-item mode (itemId set): PATCH the existing item instead of
  // creating a new one, then return to it — never mints a new plan item.
  const submitSelection = useCallback(
    async (kind: PickerEditFormat, style: MontagePreset) => {
      if (creating) return;

      if (itemId) {
        setCreating(true);
        setError(null);
        try {
          await updatePlanItem(itemId, {
            edit_format: persistedEditFormatFor(kind),
            ...(kind === "montage"
              ? { content_mode: "existing_footage" as const, montage_preset: style }
              : {}),
          });
          router.push(`/plan/items/${itemId}?setup=done`);
        } catch {
          setError("We couldn’t save this format. Check your connection and try again.");
          setCreating(false);
        }
        return;
      }

      if (!plan) return;
      setCreating(true);
      setError(null);
      let newItemId: string;
      try {
        const item = await addIdea(plan.id, TYPE_COPY[kind].label);
        newItemId = item.id;
      } catch {
        setError("We couldn’t create this video. Check your connection and try again.");
        setCreating(false);
        return;
      }
      try {
        await updatePlanItem(newItemId, {
          edit_format: persistedEditFormatFor(kind),
          ...(kind === "montage"
            ? { content_mode: "existing_footage" as const, montage_preset: style }
            : {}),
        });
        router.push(`/plan/items/${newItemId}?setup=done`);
      } catch {
        // Item exists but the type didn't stick — land on the item page with the
        // TYPE rail open so the user can re-pick there. No dead end.
        router.push(`/plan/items/${newItemId}`);
      }
    },
    [creating, plan, router, itemId],
  );

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
  // Edit-existing-item mode: hold the picker until the item's real kind/style
  // loads, so cards never flash the montage/classic defaults for an item that
  // is actually something else.
  if (itemId && itemLoadState === "loading") {
    return <LightShell size="narrow">{null}</LightShell>;
  }

  const onStyleStep = step === "style";
  // The kind step is the top of the creation flow in BOTH modes. In item mode
  // it must NOT link back to the item page — the item page's own Back leads
  // here, so that pairing would be a two-page loop with no exit.
  const kindStepHref = "/plan";
  const kindStepLabel = "Back to your videos";

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto flex min-h-screen max-w-[900px] flex-col px-6 pt-6">
        <div className="flex items-center justify-between">
          {onStyleStep ? (
            /* In-app back only: the kind→style transition is local state, so a
               hardware/gesture back exits /plan/new entirely (accepted trade-off
               — a shallow history entry per step isn't worth the App Router
               complexity; nothing is created until a final card tap). */
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => setStep("kind")}
              aria-label="Back to video kind"
              className="text-[20px] leading-none text-[#3f3f46]"
            >
              <ChevronLeft className="size-5" aria-hidden="true" />
            </Button>
          ) : (
            <Button variant="ghost" size="icon" asChild className="text-[22px] leading-none text-[#3f3f46]">
              <Link href={kindStepHref} aria-label={kindStepLabel}>
                ×
              </Link>
            </Button>
          )}
          <span className="text-[12px] text-[#71717a]">
            {onStyleStep ? `Step 2 of ${totalSteps}` : `Step 1 of ${totalSteps}`}
          </span>
          <span aria-hidden="true" className="h-11 w-11" />
        </div>

        {onStyleStep ? (
          <>
            <h1 className="font-display mt-6 text-[30px] font-medium leading-tight text-[#0c0c0e]">
              Choose a visual style
            </h1>
            <p className="mt-1.5 text-sm text-[#71717a]">Choose how your footage should be arranged.</p>

            <div
              className="scrollbar-none mt-6 grid grid-cols-2 gap-3.5 pb-4 sm:grid-cols-3"
              role="radiogroup"
              aria-label="Montage style"
              onKeyDown={radioGroupKeyDown}
            >
              {STYLE_TILES.map((tile) => (
                <MediaRadioCard
                  key={tile.value}
                  active={selectedStyle === tile.value}
                  saving={creating || planState !== "ready"}
                  poster={tile.poster}
                  video={tile.video}
                  scrim="h-1/2"
                  label={tile.label}
                  desc={tile.desc}
                  onSelect={() => {
                    setSelectedStyle(tile.value);
                    void submitSelection("montage", tile.value);
                  }}
                />
              ))}
            </div>
          </>
        ) : (
          <>
            <h1 className="font-display mt-6 text-[30px] font-medium leading-tight text-[#0c0c0e]">
              What do you want to make?
            </h1>
            <p className="mt-1.5 text-sm text-[#71717a]">
              Choose a format. You can change it later.
            </p>

            <div
              className="scrollbar-none -mx-6 mt-6 flex snap-x snap-mandatory gap-3.5 overflow-x-auto px-6 py-1 [scroll-padding-inline:1.5rem] sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:p-0 lg:grid-cols-3"
              role="radiogroup"
              aria-label="What kind of video"
              onKeyDown={radioGroupKeyDown}
            >
              {typeValues.map((value) => (
                <MediaRadioCard
                  key={value}
                  active={selected === value}
                  saving={creating || planState !== "ready"}
                  poster={TYPE_MEDIA[value].poster}
                  video={TYPE_MEDIA[value].video}
                  scrim="h-3/5"
                  label={TYPE_COPY[value].label}
                  desc={TYPE_COPY[value].desc}
                  meta={TYPE_COPY[value].meta}
                  onSelect={() => {
                    setSelected(value);
                    if (value === "montage") {
                      setStep("style");
                    } else {
                      void submitSelection(value, selectedStyle);
                    }
                  }}
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
      </div>
    </div>
  );
}
