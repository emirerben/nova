"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { MoreHorizontal, RotateCcw, Send, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { LightShell } from "../_components/ui/LightShell";
import SignInPrompt from "../_components/SignInPrompt";
import { Switch } from "@/components/ui/switch";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  acceptCreatorMemorySuggestion,
  clearCreatorMemory,
  CREATOR_MEMORY_ENABLED,
  createCreatorMemoryItem,
  dismissCreatorMemorySuggestion,
  forgetCreatorMemoryItem,
  getCreatorMemory,
  MemoryApiError,
  toggleCreatorMemory,
  undoCreatorMemoryOperation,
  updateCreatorMemoryItem,
  type CreatorMemoryItem,
  type CreatorMemoryResponse,
  type MemorySection,
} from "@/lib/memory-api";

const SECTION_TITLES: Record<string, string> = {
  content: "About your videos",
  video_style: "Visual style",
  stories_pacing: "Storytelling and tone",
  avoid: "Things to avoid",
  other: "Other preferences",
};
const SECTION_ORDER = ["content", "video_style", "stories_pacing", "avoid", "other"] as const;

type Status = "loading" | "ready" | "error";
type InstructionReview = {
  instruction: string;
  enforcement: "constraint" | "default";
  status: "enforced" | "advisory";
  scope: string;
};

type ProfileUndo = { operationId: string; revision: number; expiresAt: string };
function sectionsFromResponse(data: CreatorMemoryResponse): MemorySection[] {
  const byKey = new Map((data.sections ?? []).map((section) => [section.key, section]));
  for (const item of data.items ?? []) {
    const section = byKey.get(item.section) ?? { key: item.section, title: SECTION_TITLES[item.section], items: [] };
    if (!section.items.some((candidate) => candidate.id === item.id)) section.items.push(item);
    byKey.set(item.section, section);
  }
  const compatibility = data.compatibility;
  const compatibilityStatements: Array<[string, CreatorMemoryItem["section"], string | null | undefined]> = [
    ["summary", "content", compatibility?.summary ?? compatibility?.about_your_videos],
    ["goal", "content", compatibility?.goal ? `Goal: ${compatibility.goal}` : null],
    ["audience", "content", compatibility?.audience ? `Audience: ${compatibility.audience}` : null],
    ["pillars", "content", compatibility?.content_pillars?.length ? `Content pillars: ${compatibility.content_pillars.join(", ")}` : null],
    ["cadence", "content", compatibility?.posting_cadence ? `Posting cadence: ${compatibility.posting_cadence}` : null],
    ["tone", "stories_pacing", compatibility?.tone ? `Tone: ${compatibility.tone}` : null],
  ];
  for (const [id, sectionKey, text] of compatibilityStatements) {
    if (!text?.trim()) continue;
    const section = byKey.get(sectionKey) ?? { key: sectionKey, title: SECTION_TITLES[sectionKey], items: [] };
    section.items.push({
      id: `compatibility-${id}`,
      section: sectionKey,
      instruction: text.trim(),
      display_text: text.trim(),
      enforcement: "advisory",
      enforcement_status: "advisory",
      scope_label: "",
      state: "active",
      user_locked: false,
    });
    byKey.set(sectionKey, section);
  }
  return SECTION_ORDER
    .map((key) => byKey.get(key))
    .filter((section): section is MemorySection => Boolean(section?.items.length));
}

function displayItem(item: CreatorMemoryItem): string {
  return item.display_text?.trim() || item.instruction;
}

function statusText(item: CreatorMemoryItem): string | null {
  if (item.enforcement_status === "conflicted") return `Conflict · ${item.conflict?.message ?? "Review this preference"}`;
  if (item.enforcement_status === "unsupported") return "Not supported in every video format";
  if (item.enforcement_status === "advisory" || item.enforcement === "advisory") return "Advisory · Kria uses this as guidance";
  return null;
}

export default function PersonalizationPage() {
  const { status: sessionStatus } = useSession();
  const [status, setStatus] = useState<Status>("loading");
  const [data, setData] = useState<CreatorMemoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [savingToggle, setSavingToggle] = useState(false);
  const [draft, setDraft] = useState("");
  const [review, setReview] = useState<InstructionReview | null>(null);
  const [composerBusy, setComposerBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [itemBusy, setItemBusy] = useState<string | null>(null);
  const [clearBusy, setClearBusy] = useState(false);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [profileUndo, setProfileUndo] = useState<ProfileUndo | null>(null);
  const [profileUndoExpired, setProfileUndoExpired] = useState(false);
  const focusAfterMutation = useRef<string | null>(null);

  const load = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const memory = await getCreatorMemory();
      setData(memory);
      setStatus("ready");
    } catch (reason) {
      setStatus("error");
      setError(reason instanceof MemoryApiError && reason.code === "creator_memory_disabled"
        ? "Personalization is not available right now."
        : "Personalization couldn’t load. Your saved preferences are safe.");
    }
  }, []);

  useEffect(() => {
    if (sessionStatus === "unauthenticated") {
      window.location.replace("/plan");
    }
  }, [sessionStatus]);

  useEffect(() => {
    if (sessionStatus === "authenticated") void load();
  }, [load, sessionStatus, attempt]);

  useEffect(() => {
    if (!focusAfterMutation.current) return;
    document.getElementById(focusAfterMutation.current)?.focus();
    focusAfterMutation.current = null;
  }, [data]);

  useEffect(() => {
    if (!profileUndo) {
      setProfileUndoExpired(false);
      return undefined;
    }
    const expiresAt = Date.parse(profileUndo.expiresAt);
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
      setProfileUndoExpired(true);
      return undefined;
    }
    setProfileUndoExpired(false);
    const timer = window.setTimeout(() => setProfileUndoExpired(true), expiresAt - Date.now());
    return () => window.clearTimeout(timer);
  }, [profileUndo]);

  const sections = useMemo(() => data ? sectionsFromResponse(data) : [], [data]);
  const activeItems = sections.flatMap((section) => section.items);
  const suggestions = data?.suggestions ?? [];

  async function applyMutation(result: { memory?: CreatorMemoryResponse; revision?: number; operation_id?: string; undo_expires_at?: string | null }, captureUndo = true) {
    if (result.memory) setData(result.memory);
    else {
      const refreshed = await getCreatorMemory();
      setData(refreshed);
    }
    if (captureUndo && result.operation_id && result.undo_expires_at && result.revision !== undefined) {
      setProfileUndo({ operationId: result.operation_id, revision: result.revision, expiresAt: result.undo_expires_at });
    }
    setNotice("Personalization updated.");
    window.setTimeout(() => setNotice(null), 3500);
  }

  async function changeEnabled(enabled: boolean) {
    if (!data || savingToggle) return;
    const previous = data.enabled;
    setData({ ...data, enabled });
    setSavingToggle(true);
    try {
      await applyMutation(await toggleCreatorMemory(enabled, data.revision));
    } catch {
      setData({ ...data, enabled: previous });
      setError("Personalization couldn’t be updated. Try again.");
    } finally {
      setSavingToggle(false);
    }
  }

  async function saveNewInstruction() {
    if (!data || !review || composerBusy) return;
    setComposerBusy(true);
    setError(null);
    try {
      const result = await createCreatorMemoryItem({ instruction: review.instruction, expected_revision: data.revision, enforcement: review.enforcement });
      await applyMutation(result);
      setDraft("");
      setReview(null);
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "That preference couldn’t be saved. Try again.");
    } finally {
      setComposerBusy(false);
    }
  }

  async function saveEdit(item: CreatorMemoryItem) {
    if (!data || !editingText.trim() || itemBusy) return;
    setItemBusy(item.id);
    try {
      focusAfterMutation.current = `memory-item-${item.id}`;
      if (item.id.startsWith("compatibility-")) {
        await applyMutation(await createCreatorMemoryItem({ instruction: editingText.trim(), expected_revision: data.revision, category: item.section, enforcement: inferEnforcement(editingText), compatibility_key: item.id.slice("compatibility-".length) as "summary" | "goal" | "audience" | "pillars" | "cadence" | "tone" }));
      } else {
        await applyMutation(await updateCreatorMemoryItem(item.id, { instruction: editingText.trim(), expected_revision: data.revision, category: item.section, enforcement: item.enforcement, normalized_key: item.normalized_key }));
      }
      setEditingId(null);
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "That preference couldn’t be updated. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function stopUsing(item: CreatorMemoryItem) {
    if (!data || itemBusy) return;
    setItemBusy(item.id);
    try {
      focusAfterMutation.current = `memory-item-${item.id}`;
      const result = await forgetCreatorMemoryItem(item.id, data.revision);
      await applyMutation(result);
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "That preference couldn’t be stopped. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function acceptSuggestion(item: CreatorMemoryItem) {
    if (!data || itemBusy) return;
    setItemBusy(item.id);
    try {
      focusAfterMutation.current = `memory-item-${item.id}`;
      await applyMutation(await acceptCreatorMemorySuggestion(item.id, data.revision));
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "That suggestion couldn’t be remembered. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function dismissSuggestion(item: CreatorMemoryItem) {
    if (itemBusy) return;
    setItemBusy(item.id);
    try {
      await applyMutation(await dismissCreatorMemorySuggestion(item.id, data?.revision ?? 0));
    } catch {
      setError("That suggestion couldn’t be dismissed. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function clearRememberedPreferences() {
    if (!data || clearBusy || activeItems.length === 0) return;
    setClearBusy(true);
    setError(null);
    try {
      await applyMutation(await clearCreatorMemory(data.revision));
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "Remembered preferences couldn’t be cleared. Try again.");
    } finally {
      setClearBusy(false);
    }
  }

  async function undoAutomaticMemory() {
    if (!data?.recent_undo) return;
    setItemBusy(data.recent_undo.item_id ?? data.recent_undo.operation_id);
    try {
      await applyMutation(await undoCreatorMemoryOperation(data.recent_undo.operation_id, data.revision));
    } catch {
      setError("This change can’t be undone because Personalization has changed. Review the current preferences.");
    } finally {
      setItemBusy(null);
    }
  }

  async function undoProfileMutation() {
    if (!data || !profileUndo || profileUndoExpired || itemBusy) return;
    setItemBusy(profileUndo.operationId);
    try {
      const operation = profileUndo;
      const result = await undoCreatorMemoryOperation(operation.operationId, operation.revision);
      setProfileUndo(null);
      await applyMutation(result, false);
      setNotice("Personalization change undone.");
    } catch (reason) {
      setError(reason instanceof MemoryApiError && reason.code === "undo_expired"
        ? "This 10-minute Undo window has expired."
        : "This change can’t be undone because Personalization has changed.");
    } finally {
      setItemBusy(null);
    }
  }

  if (sessionStatus === "loading" || (sessionStatus === "authenticated" && status === "loading")) {
    return <PersonalizationSkeleton />;
  }

  if (sessionStatus === "unauthenticated") {
    return <LightShell><SignInPrompt callbackUrl="/plan/profile" title="Sign in to view Personalization" subtitle="Your creator preferences are private and only available to your account." /></LightShell>;
  }

  if (!CREATOR_MEMORY_ENABLED) {
    return <PersonalizationUnavailable />;
  }

  if (status === "error" || !data) {
    return (
      <LightShell size="narrow">
        <div className="py-8" role="alert">
          <h1 className="font-display text-3xl text-[#0c0c0e]">Personalization couldn’t load</h1>
          <p className="mt-3 text-sm text-[#71717a]">{error ?? "Try again in a moment."}</p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Button type="button" variant="ink" onClick={() => setAttempt((value) => value + 1)}><RotateCcw aria-hidden="true" /> Retry</Button>
            <Button asChild type="button" variant="outline"><Link href="/plan">Back to content plan</Link></Button>
          </div>
        </div>
      </LightShell>
    );
  }

  return (
    <LightShell size="narrow">
      <main aria-labelledby="personalization-title">
        <header className="flex items-center justify-between border-b border-zinc-200 pb-5">
          <div className="flex min-w-0 items-baseline gap-2"><h1 id="personalization-title" className="text-balance font-display text-3xl text-[#0c0c0e]">Personalization</h1><span className="truncate text-xs text-[#71717a]">{lastUpdatedLabel(data)}</span></div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0" aria-label="Personalization options"><MoreHorizontal /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => void changeEnabled(!data.enabled)}>{data.enabled ? "Pause personalization" : "Turn on personalization"}</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem disabled={clearBusy || activeItems.length === 0} onSelect={() => setClearConfirmOpen(true)}>Clear remembered preferences</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setNotice("Personalization applies to future projects and can be changed here at any time.")}>Learn about personalization</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </header>

        {error && <div className="mt-5 flex flex-wrap items-center gap-3 border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]" role="alert"><span>{error}</span><Button type="button" variant="outline" size="sm" onClick={() => setAttempt((value) => value + 1)}>Retry</Button></div>}
        {notice && <p className="mt-4 text-sm text-lime-700" role="status" aria-live="polite">{notice}</p>}
        {profileUndo && !profileUndoExpired && <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-y border-zinc-200 py-3 text-sm text-[#3f3f46]" role="status"><span>Personalization changed. Undo is available for 10 minutes.</span><Button type="button" variant="link" className="min-h-11 p-0 text-sm" onClick={() => void undoProfileMutation()} disabled={Boolean(itemBusy)}><Undo2 className="size-4" /> Undo</Button></div>}
        {data.recent_undo && <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-y border-zinc-200 py-3 text-sm text-[#3f3f46]" role="status"><span>Kria remembered a lasting instruction from a recent project.</span><Button type="button" variant="link" className="min-h-11 p-0 text-sm" onClick={() => void undoAutomaticMemory()} disabled={Boolean(itemBusy)}><Undo2 className="size-4" /> Undo</Button></div>}

        <section className="border-b border-zinc-200 py-6" aria-label="Personalization settings">
          <div className="flex items-center justify-between gap-6"><div><h2 className="text-sm font-medium text-[#0c0c0e]">Use personalization in new projects</h2><p className="mt-1 max-w-xl text-sm text-[#71717a]">Kria uses what it knows about you and learns from lasting instructions you give it.</p></div><Switch checked={data.enabled} onCheckedChange={changeEnabled} disabled={savingToggle} aria-label="Use personalization in new projects" /></div>
        </section>

        <div className="divide-y divide-zinc-200">
          {sections.map((section) => <section key={section.key} className="py-6" aria-labelledby={`section-${section.key}`}>
            <h2 id={`section-${section.key}`} className="text-balance text-sm font-semibold text-[#0c0c0e]">{section.title}</h2>
            <div className="mt-2 space-y-4" role="list" aria-label={section.title}>{section.items.map((item) => <MemoryStatement key={item.id} item={item} editing={editingId === item.id} editingText={editingText} busy={itemBusy === item.id} onEdit={() => { setEditingId(item.id); setEditingText(displayItem(item)); }} onCancel={() => setEditingId(null)} onChange={setEditingText} onSave={() => void saveEdit(item)} onStop={() => void stopUsing(item)} />)}</div>
          </section>)}
        </div>

        {suggestions.length > 0 && <section className="border-t border-zinc-200 py-6" aria-labelledby="suggestion-heading"><h2 id="suggestion-heading" className="text-sm font-semibold text-[#0c0c0e]">Suggestion</h2>{suggestions.map((item) => <div key={item.id} className="mt-2" id={`memory-item-${item.id}`} tabIndex={-1}><p className="text-sm text-[#3f3f46]">{displayItem(item)}</p><p className="mt-1 text-sm text-[#71717a]">{item.conflict?.message ?? "Not used in future videos until you accept it."}</p><div className="mt-2 flex flex-wrap gap-x-4 gap-y-2"><Button type="button" variant="link" className="min-h-11 p-0 text-xs" onClick={() => setNotice(item.source_label ? `Suggested from ${item.source_label}.` : "Kria noticed this pattern in your explicit feedback.")}>Why this suggestion</Button><Button type="button" variant="link" className="min-h-11 p-0 text-xs" disabled={itemBusy === item.id} onClick={() => void acceptSuggestion(item)}>{item.conflict ? "Use this instead" : "Remember"}</Button><Button type="button" variant="link" className="min-h-11 p-0 text-xs" disabled={itemBusy === item.id} onClick={() => void dismissSuggestion(item)}>{item.conflict ? "Keep current" : "Dismiss"}</Button></div></div>)}</section>}

        {activeItems.length === 0 && suggestions.length === 0 && <div className="border-y border-zinc-200 py-8" role="status"><h2 className="text-sm font-semibold text-[#0c0c0e]">Nothing remembered yet</h2><p className="mt-2 max-w-lg text-sm leading-relaxed text-[#71717a]">Tell Kria how you like your videos to look, sound, and feel. Explicit instructions become part of every new project and can be changed here.</p></div>}
        <p className="py-5 text-sm text-[#71717a]"><strong className="font-medium text-[#3f3f46]">{data.enabled ? "Used automatically in new projects." : "Personalization is paused."}</strong> Project instructions can override these preferences without changing Personalization.</p>
        <section className="border-t border-zinc-200 pt-5" aria-label="Tell Kria what to remember">
          <form onSubmit={(event) => { event.preventDefault(); if (draft.trim()) setReview(reviewInstruction(draft)); }} className="flex items-center gap-2 rounded-2xl border border-zinc-200 bg-white p-2 pl-4 shadow-sm focus-within:border-lime-600/60">
            <Input value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={500} placeholder="Tell Kria what to remember" aria-label="Tell Kria what to remember" className="h-10 border-0 bg-transparent px-0 shadow-none focus-visible:ring-0" disabled={composerBusy} />
            <Button type="submit" variant="ink" size="icon" className="size-10 shrink-0 rounded-full" aria-label="Review memory update" disabled={!draft.trim() || composerBusy}><Send className="size-4" /></Button>
          </form>
          {review && <div className="mt-3 border border-lime-200 bg-lime-50 p-4" role="region" aria-label="Memory update review"><p className="text-sm font-medium text-[#0c0c0e]">Remember this for future videos?</p><p className="mt-2 text-sm text-[#3f3f46]">“{review.instruction}”</p><dl className="mt-3 grid gap-2 text-xs text-[#3f3f46] sm:grid-cols-3"><div><dt className="text-[#71717a]">Scope</dt><dd className="font-medium">{review.scope}</dd></div><div><dt className="text-[#71717a]">Enforcement</dt><dd className="font-medium">{review.status === "enforced" ? "Enforced" : "Advisory"}</dd></div><div><dt className="text-[#71717a]">Saved as</dt><dd className="font-medium">{review.enforcement === "constraint" ? "Always / never rule" : "Default preference"}</dd></div></dl><p className="mt-3 text-xs leading-relaxed text-[#3f3f46]">{review.status === "enforced" ? "Kria can apply this rule to supported video controls." : "Kria will use this as guidance and may ask before applying it where support is unclear."}</p><div className="mt-3 flex flex-wrap gap-2"><Button type="button" variant="ink" size="sm" onClick={() => void saveNewInstruction()} disabled={composerBusy}>{composerBusy ? "Saving…" : "Save"}</Button><Button type="button" variant="outline" size="sm" onClick={() => setReview(null)} disabled={composerBusy}>Keep editing</Button></div></div>}
        </section>
        <p className="pt-5 text-xs leading-relaxed text-[#8a8a8a]">Personalization applies to future projects. Kria learns from your messages and explicit feedback, not uploaded-media text or assistant output.</p>
      </main>
      <AlertDialog open={clearConfirmOpen} onOpenChange={(open) => { if (!clearBusy) setClearConfirmOpen(open); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Clear remembered preferences?</AlertDialogTitle>
            <AlertDialogDescription>This removes saved video preferences from future projects. Your creator background and goals stay here.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={clearBusy}>Cancel</AlertDialogCancel>
            <AlertDialogAction disabled={clearBusy} onClick={(event) => { event.preventDefault(); void clearRememberedPreferences().then(() => setClearConfirmOpen(false)); }}>{clearBusy ? "Clearing…" : "Clear preferences"}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </LightShell>
  );
}

function MemoryStatement({ item, editing, editingText, busy, onEdit, onCancel, onChange, onSave, onStop }: { item: CreatorMemoryItem; editing: boolean; editingText: string; busy: boolean; onEdit: () => void; onCancel: () => void; onChange: (value: string) => void; onSave: () => void; onStop: () => void }) {
  const compatibilityOnly = item.id.startsWith("compatibility-");
  return (
    <article className="relative py-1" role="listitem" id={`memory-item-${item.id}`} tabIndex={-1}>
      <div className="pr-14">
        {editing ? (
          <div className="space-y-2">
            <Input value={editingText} onChange={(event) => onChange(event.target.value)} maxLength={500} aria-label={`Edit ${SECTION_TITLES[item.section]}`} autoFocus />
            <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="ink" onClick={onSave} disabled={busy || !editingText.trim()}>Save</Button><Button type="button" size="sm" variant="outline" onClick={onCancel} disabled={busy}>Cancel</Button></div>
          </div>
        ) : <><p className="text-pretty text-sm leading-relaxed text-[#3f3f46]">{displayItem(item)}</p>{statusText(item) && <p className="mt-1 text-xs text-[#71717a]">{statusText(item)}</p>}{item.scope_label !== "" && <p className="mt-1 text-xs text-[#71717a]">Applies to all future videos{item.source_label ? ` · ${item.source_label}` : ""}{item.source_deleted ? " · Source project deleted" : ""}</p>}{item.source_thread_id && !item.source_deleted ? <Button asChild type="button" variant="link" className="mt-1 h-auto p-0 text-xs"><Link href={`/plan/${encodeURIComponent(item.source_thread_id)}`}>View source</Link></Button> : null}</>}
      </div>
      <div className="absolute right-0 top-0 flex items-center gap-1">
        <Button type="button" variant="link" className="h-10 px-1 text-xs text-[#71717a]" onClick={onEdit}>Edit</Button>
        {!compatibilityOnly && <DropdownMenu><DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" className="size-10" aria-label={`Actions for ${displayItem(item)}`}><MoreHorizontal className="size-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onSelect={onEdit}>Edit</DropdownMenuItem><DropdownMenuItem onSelect={onStop}>Stop using for future videos</DropdownMenuItem>{item.source_deleted ? <DropdownMenuItem disabled>Source project deleted</DropdownMenuItem> : null}</DropdownMenuContent></DropdownMenu>}
      </div>
    </article>
  );
}

function inferEnforcement(instruction: string): "constraint" | "default" {
  return /\b(always|never|don't|do not|avoid)\b/i.test(instruction) ? "constraint" : "default";
}

function reviewInstruction(rawInstruction: string): InstructionReview {
  const instruction = rawInstruction.trim().replace(/\s+/g, " ");
  const enforcement = inferEnforcement(instruction);
  // The review is deliberately conservative: only the typed renderer controls
  // can promise enforcement today. Other instructions remain useful guidance.
  const supported = /\b(font|typeface|shadow|drop shadow|shadows)\b/i.test(instruction);
  return {
    instruction,
    enforcement,
    status: enforcement === "constraint" && supported ? "enforced" : "advisory",
    scope: "All future videos",
  };
}

function lastUpdatedLabel(data: CreatorMemoryResponse): string {
  const timestamps = (data.items ?? [])
    .map((item) => item.updated_at)
    .filter((value): value is string => Boolean(value));
  if (!timestamps.length) return "No saved preferences yet";
  const latest = Math.max(...timestamps.map((value) => Date.parse(value)).filter(Number.isFinite));
  if (!Number.isFinite(latest)) return "Saved preferences";
  const deltaMinutes = Math.max(0, Math.round((Date.now() - latest) / 60000));
  if (deltaMinutes < 1) return "Updated just now";
  if (deltaMinutes < 60) return `Updated ${deltaMinutes}m ago`;
  const deltaHours = Math.round(deltaMinutes / 60);
  if (deltaHours < 24) return `Updated ${deltaHours}h ago`;
  return `Updated ${Math.round(deltaHours / 24)}d ago`;
}

function PersonalizationUnavailable() {
  return <LightShell size="narrow"><div className="py-8"><h1 className="text-balance font-display text-3xl text-[#0c0c0e]">Personalization isn’t available yet</h1><p className="text-pretty mt-3 max-w-md text-sm text-[#71717a]">This feature is currently paused. Your existing creator background and goals are unchanged.</p><Button asChild type="button" variant="outline" className="mt-6"><Link href="/plan">Back to content plan</Link></Button></div></LightShell>;
}

function PersonalizationSkeleton() {
  return <LightShell size="narrow"><div className="py-2 motion-safe:animate-pulse" role="status" aria-live="polite"><div className="h-9 w-52 rounded bg-zinc-100" /><div className="mt-8 h-16 border-y border-zinc-100 bg-zinc-50" /><div className="mt-8 space-y-8">{[1, 2, 3, 4].map((item) => <div key={item} className="space-y-2"><div className="h-4 w-32 rounded bg-zinc-100" /><div className="h-4 w-full rounded bg-zinc-100" /><div className="h-4 w-3/4 rounded bg-zinc-100" /></div>)}</div></div></LightShell>;
}
