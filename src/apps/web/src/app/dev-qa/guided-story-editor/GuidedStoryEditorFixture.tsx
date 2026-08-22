"use client";

import { useState } from "react";

type Clip = {
  id: string;
  label: string;
  inS: number;
  outS: number;
  look: "none" | "warm" | "mono";
  transition: "cut" | "crossfade";
};

type EditorState = { clips: Clip[]; musicTrackId: string | null };

const INITIAL_STATE: EditorState = {
  clips: [
    { id: "clip-1", label: "Clip 1", inS: 0.5, outS: 5.5, look: "none", transition: "cut" },
    { id: "clip-2", label: "Clip 2 (voiceover locked)", inS: 0, outS: 4, look: "none", transition: "cut" },
  ],
  musicTrackId: "story-bed-01",
};

const REVISION_ID = "guided-story-fixture-revision-018";
const GENERATION_ID = "guided-story-fixture-generation-018";

function cloneState(state: EditorState): EditorState {
  return { ...state, clips: state.clips.map((clip) => ({ ...clip })) };
}

export default function GuidedStoryEditorFixture() {
  const [state, setState] = useState<EditorState>(() => cloneState(INITIAL_STATE));
  const [past, setPast] = useState<EditorState[]>([]);
  const [future, setFuture] = useState<EditorState[]>([]);
  const [commitPayload, setCommitPayload] = useState<string | null>(null);

  function apply(next: (current: EditorState) => EditorState) {
    setPast((items) => [...items, cloneState(state)]);
    setFuture([]);
    setState((current) => next(cloneState(current)));
    setCommitPayload(null);
  }

  function undo() {
    const previous = past.at(-1);
    if (!previous) return;
    setFuture((items) => [...items, cloneState(state)]);
    setPast((items) => items.slice(0, -1));
    setState(cloneState(previous));
    setCommitPayload(null);
  }

  function redo() {
    const next = future.at(-1);
    if (!next) return;
    setPast((items) => [...items, cloneState(state)]);
    setFuture((items) => items.slice(0, -1));
    setState(cloneState(next));
    setCommitPayload(null);
  }

  const firstClip = state.clips[0];
  const commit = {
    base_generation: GENERATION_ID,
    guided_revision_number: 18,
    timeline_slots: state.clips.map(({ id, inS, outS, look, transition }) => ({
      slot_id: id,
      in_s: inS,
      duration_s: outS - inS,
      look_preset: look,
      transition_after: transition,
    })),
    remove_music: state.musicTrackId == null,
  };

  return (
    <main className="min-h-screen bg-[#f7f7f5] p-8 text-[#0c0c0e]">
      <div className="mx-auto max-w-6xl">
        <header className="flex items-center justify-between rounded-xl border border-zinc-200 bg-white px-6 py-5">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-zinc-500">Guided Story V2 · deterministic fixture</p>
            <h1 className="mt-1 font-display text-2xl">Story-native editor</h1>
          </div>
          <div className="flex gap-2">
            <button type="button" aria-label="Undo" disabled={past.length === 0} onClick={undo} className="rounded-lg border border-zinc-300 px-4 py-2 text-sm disabled:opacity-40">Undo</button>
            <button type="button" aria-label="Redo" disabled={future.length === 0} onClick={redo} className="rounded-lg border border-zinc-300 px-4 py-2 text-sm disabled:opacity-40">Redo</button>
            <button type="button" onClick={() => setCommitPayload(JSON.stringify(commit))} className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white">Save</button>
          </div>
        </header>

        <section className="mt-6 grid grid-cols-[1.2fr_0.8fr] gap-6">
          <div className="rounded-xl border border-zinc-200 bg-white p-6">
            <div className="flex items-center justify-between"><h2 className="font-display text-lg">Timeline</h2><span className="text-xs text-zinc-500">Output duration 9.0s</span></div>
            <div className="mt-5 space-y-3" data-testid="guided-story-timeline">
              {state.clips.map((clip, index) => {
                const editable = index === 0;
                return (
                  <div key={clip.id} className="rounded-lg border border-zinc-200 p-4">
                    <div className="flex items-center justify-between"><span className="text-sm font-semibold">{clip.label}</span><span className="text-xs tabular-nums text-zinc-500">{clip.inS.toFixed(1)}–{clip.outS.toFixed(1)}s · {(clip.outS - clip.inS).toFixed(1)}s</span></div>
                    <div className="mt-3 flex items-end gap-3">
                      <label className="flex-1 text-xs text-zinc-500">In<input aria-label={`${clip.label} In`} type="number" step="0.5" value={clip.inS} disabled={!editable} onChange={(event) => { const value = Number(event.target.value); if (!Number.isFinite(value)) return; apply((current) => ({ ...current, clips: current.clips.map((item) => item.id === clip.id ? { ...item, inS: value } : item) })); }} className="mt-1 h-10 w-full rounded border border-zinc-300 px-2 text-sm text-zinc-900 disabled:bg-zinc-100" /></label>
                      <label className="flex-1 text-xs text-zinc-500">Out<input aria-label={`${clip.label} Out`} type="number" step="0.5" value={clip.outS} disabled className="mt-1 h-10 w-full rounded border border-zinc-300 bg-zinc-100 px-2 text-sm text-zinc-900" /></label>
                      <button type="button" aria-label={`Trim ${clip.label} left to 1.0 seconds`} disabled={!editable} onClick={() => apply((current) => ({ ...current, clips: current.clips.map((item) => item.id === clip.id ? { ...item, inS: 1.0 } : item) }))} className="h-10 rounded border border-zinc-300 px-3 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40">Trim left → 1.0s</button>
                    </div>
                    {!editable && <p className="mt-3 text-xs text-amber-700" role="status" data-testid="disabled-operation-reason">Trim unavailable: locked to your voiceover</p>}
                  </div>
                );
              })}
            </div>
          </div>

          <aside className="space-y-6">
            <section className="rounded-xl border border-zinc-200 bg-white p-6">
              <h2 className="font-display text-lg">Clip 1 inspector</h2>
              <label className="mt-4 block text-xs text-zinc-500">Transition out<select aria-label="Clip 1 transition" value={firstClip.transition} onChange={(event) => apply((current) => ({ ...current, clips: current.clips.map((clip, index) => index === 0 ? { ...clip, transition: event.target.value as Clip["transition"] } : clip) }))} className="mt-1 h-10 w-full rounded border border-zinc-300 px-2 text-sm text-zinc-900"><option value="cut">Cut</option><option value="crossfade">Crossfade</option></select></label>
              <label className="mt-4 block text-xs text-zinc-500">Look<select aria-label="Clip 1 Look" value={firstClip.look} onChange={(event) => apply((current) => ({ ...current, clips: current.clips.map((clip, index) => index === 0 ? { ...clip, look: event.target.value as Clip["look"] } : clip) }))} className="mt-1 h-10 w-full rounded border border-zinc-300 px-2 text-sm text-zinc-900"><option value="none">None</option><option value="warm">Warm</option><option value="mono">Mono</option></select></label>
            </section>
            <section className="rounded-xl border border-zinc-200 bg-white p-6">
              <div className="flex items-center justify-between"><h2 className="font-display text-lg">Music</h2><span className="text-xs text-zinc-500">Continuous bed</span></div>
              <p className="mt-3 text-sm text-zinc-600">{state.musicTrackId ?? "No music selected"}</p>
              <button type="button" aria-label="Remove music" disabled={state.musicTrackId == null} onClick={() => apply((current) => ({ ...current, musicTrackId: null }))} className="mt-4 h-10 rounded border border-zinc-300 px-3 text-xs font-semibold disabled:opacity-40">Remove music</button>
            </section>
          </aside>
        </section>

        <div id="qa-state" className="sr-only" data-clip-in={firstClip.inS} data-clip-out={firstClip.outS} data-clip-duration={firstClip.outS - firstClip.inS} data-transition={firstClip.transition} data-look={firstClip.look} data-music-removed={state.musicTrackId == null} data-past-len={past.length} data-future-len={future.length} data-revision-id={REVISION_ID} data-generation-id={GENERATION_ID} data-commit-payload={commitPayload ?? ""} />
      </div>
    </main>
  );
}
