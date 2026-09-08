"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { ArrowLeft, MoreHorizontal, RotateCcw, Send } from "lucide-react";
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
  updateCreatorMemoryItem,
  type CreatorMemoryItem,
  type CreatorMemoryResponse,
} from "@/lib/memory-api";

type Status = "loading" | "ready" | "error";
const EDITABLE_COMPATIBILITY_KEYS = ["summary", "goal", "audience", "pillars", "cadence", "tone"] as const;
type EditableCompatibilityKey = (typeof EDITABLE_COMPATIBILITY_KEYS)[number];

function editableCompatibilityKey(item: CreatorMemoryItem): EditableCompatibilityKey | null {
  if (!item.id.startsWith("compatibility-")) return null;
  const key = item.id.slice("compatibility-".length);
  return EDITABLE_COMPATIBILITY_KEYS.includes(key as EditableCompatibilityKey) ? key as EditableCompatibilityKey : null;
}

function itemsFromResponse(data: CreatorMemoryResponse): CreatorMemoryItem[] {
  const items = [...(data.items ?? [])];
  const compatibilityItems: CreatorMemoryItem[] = [];
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
    compatibilityItems.push({
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
  }
  return [...compatibilityItems, ...items];
}

function displayItem(item: CreatorMemoryItem): string {
  return item.display_text?.trim() || item.instruction;
}

export default function PersonalizationPage() {
  const { status: sessionStatus } = useSession();
  const [status, setStatus] = useState<Status>("loading");
  const [data, setData] = useState<CreatorMemoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [savingToggle, setSavingToggle] = useState(false);
  const [draft, setDraft] = useState("");
  const [composerBusy, setComposerBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [itemBusy, setItemBusy] = useState<string | null>(null);
  const [clearBusy, setClearBusy] = useState(false);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const focusAfterMutation = useRef<string | null>(null);
  const [focusRequest, setFocusRequest] = useState(0);

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
    const targetId = focusAfterMutation.current;
    if (!targetId) return;
    (document.getElementById(targetId) ?? document.getElementById("memory-composer"))?.focus();
    focusAfterMutation.current = null;
  }, [focusRequest]);

  const activeItems = useMemo(() => data ? itemsFromResponse(data) : [], [data]);
  const suggestions = data?.suggestions ?? [];

  async function applyMutation(result: { memory?: CreatorMemoryResponse }) {
    if (result.memory) setData(result.memory);
    else {
      const refreshed = await getCreatorMemory();
      setData(refreshed);
    }
  }

  async function changeEnabled(enabled: boolean) {
    if (!data || savingToggle) return;
    const previous = data.enabled;
    setData({ ...data, enabled });
    setSavingToggle(true);
    setError(null);
    try {
      await applyMutation(await toggleCreatorMemory(enabled, data.revision));
    } catch {
      setData((current) => current ? { ...current, enabled: previous } : current);
      setError("Personalization couldn’t be updated. Try again.");
    } finally {
      setSavingToggle(false);
    }
  }

  async function saveNewInstruction() {
    const instruction = draft.trim().replace(/\s+/g, " ");
    if (!data || !instruction || composerBusy) return;
    setComposerBusy(true);
    setError(null);
    try {
      const result = await createCreatorMemoryItem({ instruction, expected_revision: data.revision, enforcement: inferEnforcement(instruction) });
      await applyMutation(result);
      setDraft("");
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "That preference couldn’t be saved. Try again.");
    } finally {
      setComposerBusy(false);
    }
  }

  async function saveEdit(item: CreatorMemoryItem) {
    const instruction = editingText.trim().replace(/\s+/g, " ");
    const compatibilityKey = editableCompatibilityKey(item);
    if (!data || !instruction || itemBusy || (item.id.startsWith("compatibility-") && !compatibilityKey)) return;
    setItemBusy(item.id);
    setError(null);
    try {
      focusAfterMutation.current = `edit-memory-${item.id}`;
      const result = item.id.startsWith("compatibility-")
        ? await createCreatorMemoryItem({
          instruction,
          expected_revision: data.revision,
          category: item.section,
          enforcement: inferEnforcement(instruction),
          compatibility_key: compatibilityKey!,
        })
        : await updateCreatorMemoryItem(item.id, {
          instruction,
          expected_revision: data.revision,
          category: item.section,
          enforcement: item.enforcement,
          normalized_key: item.normalized_key,
        });
      await applyMutation(result);
      setEditingId(null);
      setFocusRequest((value) => value + 1);
    } catch (reason) {
      focusAfterMutation.current = null;
      setError(reason instanceof MemoryApiError ? reason.message : "That preference couldn’t be saved. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function stopUsing(item: CreatorMemoryItem) {
    if (!data || item.id.startsWith("compatibility-") || itemBusy) return;
    setItemBusy(item.id);
    setError(null);
    try {
      focusAfterMutation.current = `memory-item-${item.id}`;
      await applyMutation(await forgetCreatorMemoryItem(item.id, data.revision));
      setFocusRequest((value) => value + 1);
    } catch (reason) {
      focusAfterMutation.current = null;
      setError(reason instanceof MemoryApiError ? reason.message : "That preference couldn’t be removed. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function acceptSuggestion(item: CreatorMemoryItem) {
    if (!data || itemBusy) return;
    setItemBusy(item.id);
    setError(null);
    try {
      focusAfterMutation.current = `memory-item-${item.id}`;
      await applyMutation(await acceptCreatorMemorySuggestion(item.id, data.revision));
      setFocusRequest((value) => value + 1);
    } catch (reason) {
      focusAfterMutation.current = null;
      setError(reason instanceof MemoryApiError ? reason.message : "That suggestion couldn’t be remembered. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function dismissSuggestion(item: CreatorMemoryItem) {
    if (itemBusy) return;
    setItemBusy(item.id);
    setError(null);
    try {
      focusAfterMutation.current = `memory-item-${item.id}`;
      await applyMutation(await dismissCreatorMemorySuggestion(item.id, data?.revision ?? 0));
      setFocusRequest((value) => value + 1);
    } catch {
      focusAfterMutation.current = null;
      setError("That suggestion couldn’t be dismissed. Try again.");
    } finally {
      setItemBusy(null);
    }
  }

  async function clearRememberedPreferences(): Promise<boolean> {
    if (!data || clearBusy || activeItems.length === 0) return false;
    setClearBusy(true);
    setError(null);
    try {
      await applyMutation(await clearCreatorMemory(data.revision));
      return true;
    } catch (reason) {
      setError(reason instanceof MemoryApiError ? reason.message : "Remembered preferences couldn’t be cleared. Try again.");
      return false;
    } finally {
      setClearBusy(false);
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
        <header className="pb-5">
          <div className="flex items-center justify-between">
            <Button asChild type="button" variant="ghost" className="-ml-2 min-h-11 gap-2 px-2 text-[#71717a]"><Link href="/plan" aria-label="Back to content plan"><ArrowLeft aria-hidden="true" className="size-4" /><span>Back</span></Link></Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0" aria-label="Personalization options"><MoreHorizontal /></Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => void changeEnabled(!data.enabled)}>{data.enabled ? "Pause personalization" : "Turn on personalization"}</DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem disabled={clearBusy || activeItems.length === 0} onSelect={() => setClearConfirmOpen(true)}>Clear remembered preferences</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setNotice("Personalization applies to future projects and can be changed here at any time.")}>Learn about personalization</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          <h1 id="personalization-title" className="mt-3 text-balance font-display text-3xl text-[#0c0c0e]">Personalization</h1>
        </header>

        {error && <div className="mt-5 flex flex-wrap items-center gap-3 border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]" role="alert"><span>{error}</span><Button type="button" variant="outline" size="sm" onClick={() => setAttempt((value) => value + 1)}>Retry</Button></div>}
        {notice && <p className="mt-4 text-sm text-lime-700" role="status" aria-live="polite">{notice}</p>}

        <section className="border-b border-zinc-200 py-6" aria-label="Personalization settings">
          <div className="flex items-center justify-between gap-6"><div><h2 className="text-sm font-medium text-[#0c0c0e]">Use personalization in new projects</h2><p className="mt-1 max-w-xl text-sm text-[#71717a]">Kria uses what it knows about you and learns from lasting instructions you give it.</p></div><Switch className="relative after:absolute after:-inset-3 after:content-['']" checked={data.enabled} onCheckedChange={changeEnabled} disabled={savingToggle} aria-label="Use personalization in new projects" /></div>
        </section>

        {activeItems.length > 0 && <section className="border-b border-zinc-200 py-6" aria-labelledby="remembered-preferences-heading">
          <h2 id="remembered-preferences-heading" className="text-balance text-sm font-semibold text-[#0c0c0e]">What Kria remembers</h2>
          <div className="mt-2 divide-y divide-zinc-100" role="list" aria-label="Remembered preferences">
            {activeItems.map((item) => <MemoryStatement key={item.id} item={item} editing={editingId === item.id} editingText={editingText} busy={itemBusy === item.id} onEdit={() => { setEditingId(item.id); setEditingText(displayItem(item)); }} onCancel={() => setEditingId(null)} onChange={setEditingText} onSave={() => void saveEdit(item)} onStop={() => void stopUsing(item)} />)}
          </div>
        </section>}

        {suggestions.length > 0 && <section className="border-b border-zinc-200 py-6" aria-labelledby="suggestion-heading"><h2 id="suggestion-heading" className="text-sm font-semibold text-[#0c0c0e]">Suggestion</h2>{suggestions.map((item) => <div key={item.id} className="mt-2" id={`memory-item-${item.id}`} tabIndex={-1}><p className="text-sm text-[#3f3f46]">{displayItem(item)}</p><p className="mt-1 text-sm text-[#71717a]">{item.conflict?.message ?? "Not used in future videos until you accept it."}</p><div className="mt-2 flex flex-wrap gap-x-4 gap-y-2"><Button type="button" variant="link" className="min-h-11 p-0 text-xs" onClick={() => setNotice(item.source_label ? `Suggested from ${item.source_label}.` : "Kria noticed this pattern in your explicit feedback.")}>Why this suggestion</Button><Button type="button" variant="link" className="min-h-11 p-0 text-xs" disabled={itemBusy === item.id} onClick={() => void acceptSuggestion(item)}>{item.conflict ? "Use this instead" : "Remember"}</Button><Button type="button" variant="link" className="min-h-11 p-0 text-xs" disabled={itemBusy === item.id} onClick={() => void dismissSuggestion(item)}>{item.conflict ? "Keep current" : "Dismiss"}</Button></div></div>)}</section>}

        {activeItems.length === 0 && suggestions.length === 0 && <div className="border-b border-zinc-200 py-8" role="status"><h2 className="text-sm font-semibold text-[#0c0c0e]">Nothing remembered yet</h2><p className="mt-2 max-w-lg text-sm leading-relaxed text-[#71717a]">Tell Kria how you like your videos to look, sound, and feel. Explicit instructions become part of every new project and can be changed here.</p></div>}
        <p className="py-5 text-sm text-[#71717a]"><strong className="font-medium text-[#3f3f46]">{data.enabled ? "Used automatically in new projects." : "Personalization is paused."}</strong> Project instructions can override these preferences without changing Personalization.</p>
        <section className="border-t border-zinc-200 pt-5" aria-label="Tell Kria what to remember">
          <form onSubmit={(event) => { event.preventDefault(); void saveNewInstruction(); }} className="flex items-center gap-2 rounded-2xl border border-zinc-200 bg-white p-2 pl-4 shadow-sm focus-within:border-lime-600/60">
            <Input id="memory-composer" value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={500} placeholder="Tell Kria what to remember" aria-label="Tell Kria what to remember" className="h-11 border-0 bg-transparent px-0 shadow-none focus-visible:ring-0" disabled={composerBusy} />
            <Button type="submit" variant="ink" size="icon" className="size-11 shrink-0 rounded-full" aria-label="Add preference" disabled={!draft.trim() || composerBusy}><Send className="size-4" /></Button>
          </form>
        </section>
        <p className="pt-5 text-xs leading-relaxed text-[#71717a]">Personalization applies to future projects. Kria learns from your messages and explicit feedback, not uploaded-media text or assistant output.</p>
      </main>
      <AlertDialog open={clearConfirmOpen} onOpenChange={(open) => { if (!clearBusy) setClearConfirmOpen(open); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Clear remembered preferences?</AlertDialogTitle>
            <AlertDialogDescription>This removes saved video preferences from future projects. Your creator background and goals stay here.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={clearBusy}>Cancel</AlertDialogCancel>
            <AlertDialogAction disabled={clearBusy} onClick={(event) => { event.preventDefault(); void clearRememberedPreferences().then((cleared) => { if (cleared) setClearConfirmOpen(false); }); }}>{clearBusy ? "Clearing…" : "Clear preferences"}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </LightShell>
  );
}

function inferEnforcement(instruction: string): "constraint" | "default" {
  return /\b(always|never|don't|do not|avoid)\b/i.test(instruction) ? "constraint" : "default";
}

function MemoryStatement({ item, editing, editingText, busy, onEdit, onCancel, onChange, onSave, onStop }: { item: CreatorMemoryItem; editing: boolean; editingText: string; busy: boolean; onEdit: () => void; onCancel: () => void; onChange: (value: string) => void; onSave: () => void; onStop: () => void }) {
  const compatibilityOnly = item.id.startsWith("compatibility-");
  const editable = !compatibilityOnly || editableCompatibilityKey(item) !== null;
  return (
    <article className="relative py-4 first:pt-2 last:pb-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lime-700 focus-visible:ring-offset-2" role="listitem" id={`memory-item-${item.id}`} tabIndex={-1}>
      {editing ? (
        <div className="space-y-3">
          <Input value={editingText} onChange={(event) => onChange(event.target.value)} maxLength={500} aria-label={`Edit ${displayItem(item)}`} autoFocus disabled={busy} />
          <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="ink" onClick={onSave} disabled={busy || !editingText.trim()}>{busy ? "Saving…" : "Save"}</Button><Button type="button" size="sm" variant="outline" onClick={onCancel} disabled={busy}>Cancel</Button></div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-4">
          <p className="min-w-0 text-pretty text-sm leading-relaxed text-[#3f3f46]">{displayItem(item)}</p>
          {editable && <div className="flex shrink-0 items-center gap-1">
            <Button id={`edit-memory-${item.id}`} type="button" variant="link" className="min-h-11 px-1 text-xs text-[#71717a]" onClick={onEdit} aria-label={`Edit ${displayItem(item)}`}>Edit</Button>
            {!compatibilityOnly && <DropdownMenu><DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" className="size-11" aria-label={`Actions for ${displayItem(item)}`}><MoreHorizontal className="size-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onSelect={onEdit}>Edit</DropdownMenuItem><DropdownMenuItem onSelect={onStop}>Remove preference</DropdownMenuItem></DropdownMenuContent></DropdownMenu>}
          </div>}
        </div>
      )}
    </article>
  );
}

function PersonalizationUnavailable() {
  return <LightShell size="narrow"><div className="py-8"><h1 className="text-balance font-display text-3xl text-[#0c0c0e]">Personalization isn’t available yet</h1><p className="text-pretty mt-3 max-w-md text-sm text-[#71717a]">This feature is currently paused. Your existing creator background and goals are unchanged.</p><Button asChild type="button" variant="outline" className="mt-6"><Link href="/plan">Back to content plan</Link></Button></div></LightShell>;
}

function PersonalizationSkeleton() {
  return <LightShell size="narrow"><div className="py-2 motion-safe:animate-pulse" role="status" aria-live="polite"><div className="h-9 w-52 rounded bg-zinc-100" /><div className="mt-8 h-16 border-y border-zinc-100 bg-zinc-50" /><div className="mt-8 space-y-5"><div className="h-4 w-36 rounded bg-zinc-100" /><div className="h-4 w-full rounded bg-zinc-100" /><div className="h-px w-full bg-zinc-100" /><div className="h-4 w-4/5 rounded bg-zinc-100" /></div></div></LightShell>;
}
