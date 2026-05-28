"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  adminGetMusicTrack,
  adminGetAudioUrl,
  adminUpdateMusicTrack,
  adminReanalyzeMusicTrack,
  adminArchiveMusicTrack,
  type MusicTrackDetail,
  type SongSection,
  type TrackConfig,
} from "@/lib/music-api";
import { adminCreateTemplateFromMusicTrack } from "@/lib/admin-api";
import LyricsConfigPanel from "@/app/admin/_shared/LyricsConfigPanel";
import type { LyricsConfig } from "@/lib/music-api";
import { matchSectionByBounds } from "@/lib/music-section-match";
import { countSlotsClient } from "@/lib/music-slot-count";
import { AudioPlayer } from "./components/AudioPlayer";
import { LyricsTab } from "./components/LyricsTab";
import { TestTab } from "./components/TestTab";

type AdminMusicTabId = "config" | "test" | "lyrics";

const ADMIN_MUSIC_TABS: { id: AdminMusicTabId; label: string }[] = [
  { id: "config", label: "Config" },
  { id: "lyrics", label: "Lyrics" },
  { id: "test", label: "Test" },
];

// AudioPlayer extracted to ./components/AudioPlayer (Next.js page files
// only allow `default` plus a small whitelist of named exports — see
// https://nextjs.org/docs/messages/invalid-page-config).


// ── Main Page ─────────────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  queued: "bg-zinc-700 text-zinc-300",
  analyzing: "bg-blue-900 text-blue-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
};

export default function AdminMusicTrackPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeTabRaw = (searchParams.get("tab") as AdminMusicTabId) || "config";
  const activeTab: AdminMusicTabId = ADMIN_MUSIC_TABS.some((t) => t.id === activeTabRaw)
    ? activeTabRaw
    : "config";
  const setTab = useCallback(
    (tab: AdminMusicTabId) => {
      router.replace(`/admin/music/${id}?tab=${tab}`);
    },
    [id, router],
  );
  const [track, setTrack] = useState<MusicTrackDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creatingTemplate, setCreatingTemplate] = useState(false);
  // Track whether LyricsConfigPanel has unsaved edits. We block Create
  // Template on dirty state below — the failure mode this guards is the
  // user checking the lyrics box, clicking Create Template before clicking
  // Save, and getting a template the linked track doesn't carry lyrics for.
  const [lyricsDirty, setLyricsDirty] = useState(false);
  const [pendingLyricsCfg, setPendingLyricsCfg] = useState<LyricsConfig | null>(null);

  // Config form state
  const [bestStart, setBestStart] = useState("");
  const [bestEnd, setBestEnd] = useState("");
  const [slotEveryN, setSlotEveryN] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  // Reanalyze
  const [reanalyzing, setReanalyzing] = useState(false);

  // Poll while analyzing OR extracting lyrics. Lyrics extraction runs after
  // beat analysis, so we keep polling until both are out of an in-flight state.
  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;
    const inflight =
      track?.analysis_status === "analyzing" ||
      track?.analysis_status === "queued" ||
      track?.lyrics_status === "extracting" ||
      track?.lyrics_status === "pending";
    if (inflight) {
      interval = setInterval(async () => {
        try {
          const fresh = await adminGetMusicTrack(id);
          setTrack(fresh);
          syncFormFromTrack(fresh);
          const stillInflight =
            fresh.analysis_status === "analyzing" ||
            fresh.analysis_status === "queued" ||
            fresh.lyrics_status === "extracting" ||
            fresh.lyrics_status === "pending";
          if (!stillInflight) {
            clearInterval(interval);
          }
        } catch {
          // keep polling
        }
      }, 3000);
    }
    return () => clearInterval(interval);
  }, [id, track?.analysis_status, track?.lyrics_status]);

  function syncFormFromTrack(t: MusicTrackDetail) {
    const cfg = t.track_config;
    setBestStart(cfg?.best_start_s?.toString() ?? "");
    setBestEnd(cfg?.best_end_s?.toString() ?? "");
    setSlotEveryN(cfg?.slot_every_n_beats?.toString() ?? "8");
  }

  useEffect(() => {
    adminGetMusicTrack(id)
      .then((t) => {
        setTrack(t);
        syncFormFromTrack(t);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [id]);

  async function handleSaveConfig(e: React.FormEvent) {
    e.preventDefault();
    if (!track) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      const updated = await adminUpdateMusicTrack(id, {
        track_config: {
          best_start_s: parseFloat(bestStart),
          best_end_s: parseFloat(bestEnd),
          slot_every_n_beats: parseInt(slotEveryN, 10),
        },
      });
      setTrack(updated);
      // Re-sync form state from the persisted response. Without this, a user
      // who typed "127.20" stays dirty after Save (cfg.best_start_s=127.2 →
      // toString()="127.2", form keeps "127.20"), and `sectionBoundsDirty`
      // blocks the preview button indefinitely. The same trap applied to the
      // pre-existing `hasUnsavedChanges` badge but went unnoticed because
      // the badge was cosmetic; the new preview gate makes it user-blocking.
      syncFormFromTrack(updated);
      setSaveMsg("Saved.");
    } catch (e: unknown) {
      setSaveMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleTogglePublish() {
    if (!track) return;
    try {
      const updated = await adminUpdateMusicTrack(id, {
        publish: track.published_at === null,
      });
      setTrack(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update publish status");
    }
  }

  async function handleReanalyze() {
    if (!track) return;
    setReanalyzing(true);
    try {
      await adminReanalyzeMusicTrack(id);
      const fresh = await adminGetMusicTrack(id);
      setTrack(fresh);
      syncFormFromTrack(fresh);
    } finally {
      setReanalyzing(false);
    }
  }

  async function handleCreateTemplate() {
    if (!track) return;
    // Guard the unsaved-checkbox footgun. If the lyrics panel has pending
    // edits, the user almost certainly wants those persisted on the track
    // before we derive a template from it — otherwise the new template
    // inherits a config that hasn't been saved anywhere.
    let cfgToSaveFirst: LyricsConfig | null = null;
    if (lyricsDirty && pendingLyricsCfg) {
      const choice = window.confirm(
        "You have unsaved lyrics settings. Save them to the track before creating the template?\n\n" +
          "OK = Save lyrics config, then create the template (recommended)\n" +
          "Cancel = Create the template with whatever's currently saved on the track",
      );
      if (choice) {
        cfgToSaveFirst = pendingLyricsCfg;
      }
    }
    setCreatingTemplate(true);
    try {
      if (cfgToSaveFirst) {
        const updated = await adminUpdateMusicTrack(track.id, {
          track_config: {
            ...(track.track_config ?? {}),
            lyrics_config: cfgToSaveFirst,
          },
        });
        setTrack(updated);
      }
      const template = await adminCreateTemplateFromMusicTrack(track.id);
      router.push(`/admin/templates/${template.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create template");
      setCreatingTemplate(false);
    }
  }

  async function handleArchive() {
    if (!track) return;
    if (!confirm("Archive this track? It will be hidden from the gallery.")) return;
    await adminArchiveMusicTrack(id);
    const fresh = await adminGetMusicTrack(id);
    setTrack(fresh);
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <p className="text-zinc-400">Loading…</p>
      </div>
    );
  }
  if (error || !track) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <p className="text-red-400">{error ?? "Track not found"}</p>
      </div>
    );
  }

  const cfg = track.track_config ?? ({} as TrackConfig);

  // Dirty-state for the best-section bounds ONLY (not slot_every_n).
  // Computed at the page-top level because the form state lives here while
  // the lyric-preview button lives on sibling tabs (Lyrics, Test). When the
  // user clicks a section band on the Config tab's AudioPlayer, bestStart /
  // bestEnd update locally but the DB isn't touched until Save — so any
  // preview kicked off while these strings differ from the persisted
  // toString would render against stale section bounds (the Beat It bug,
  // job 616d3e53). Comparing strings avoids float formatting drift since
  // the form holds raw input strings.
  const sectionBoundsDirty =
    bestStart !== (cfg.best_start_s?.toString() ?? "") ||
    bestEnd !== (cfg.best_end_s?.toString() ?? "");

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6 max-w-3xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <Link href="/admin/music" className="text-zinc-400 hover:text-zinc-200 text-sm">
          ← Music Tracks
        </Link>
        <h1 className="text-2xl font-bold flex-1 truncate">{track.title}</h1>
        <span
          className={`text-xs font-semibold px-2 py-1 rounded-full ${
            STATUS_COLORS[track.analysis_status] ?? STATUS_COLORS.queued
          }`}
        >
          {track.analysis_status}
        </span>
      </div>

      {/* Tabs */}
      <div className="border-b border-zinc-800 mb-6">
        <div className="flex gap-1">
          {ADMIN_MUSIC_TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setTab(tab.id)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab.id
                  ? "border-white text-white"
                  : "border-transparent text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {activeTab === "test" ? (
        <TestTab trackId={id} track={track} sectionBoundsDirty={sectionBoundsDirty} />
      ) : activeTab === "lyrics" ? (
        <LyricsTab
          trackId={id}
          track={track}
          onTrackUpdated={setTrack}
          sectionBoundsDirty={sectionBoundsDirty}
        />
      ) : (
        <ConfigTabContent
          id={id}
          track={track}
          setTrack={setTrack}
          cfg={cfg}
          bestStart={bestStart}
          setBestStart={setBestStart}
          bestEnd={bestEnd}
          setBestEnd={setBestEnd}
          slotEveryN={slotEveryN}
          setSlotEveryN={setSlotEveryN}
          saving={saving}
          saveMsg={saveMsg}
          reanalyzing={reanalyzing}
          creatingTemplate={creatingTemplate}
          handleSaveConfig={handleSaveConfig}
          handleTogglePublish={handleTogglePublish}
          handleReanalyze={handleReanalyze}
          handleCreateTemplate={handleCreateTemplate}
          handleArchive={handleArchive}
          onLyricsDirtyChange={(dirty, cfg) => {
            setLyricsDirty(dirty);
            setPendingLyricsCfg(cfg);
          }}
        />
      )}
    </div>
  );
}

interface ConfigTabContentProps {
  id: string;
  track: MusicTrackDetail;
  setTrack: (t: MusicTrackDetail) => void;
  cfg: TrackConfig;
  bestStart: string;
  setBestStart: (s: string) => void;
  bestEnd: string;
  setBestEnd: (s: string) => void;
  slotEveryN: string;
  setSlotEveryN: (s: string) => void;
  saving: boolean;
  saveMsg: string | null;
  reanalyzing: boolean;
  creatingTemplate: boolean;
  handleSaveConfig: (e: React.FormEvent) => void;
  handleTogglePublish: () => void;
  handleReanalyze: () => void;
  handleCreateTemplate: () => void;
  handleArchive: () => void;
  onLyricsDirtyChange: (dirty: boolean, cfg: LyricsConfig) => void;
}

function ConfigTabContent({
  id,
  track,
  setTrack,
  cfg,
  bestStart,
  setBestStart,
  bestEnd,
  setBestEnd,
  slotEveryN,
  setSlotEveryN,
  saving,
  saveMsg,
  reanalyzing,
  creatingTemplate,
  handleSaveConfig,
  handleTogglePublish,
  handleReanalyze,
  handleCreateTemplate,
  handleArchive,
  onLyricsDirtyChange,
}: ConfigTabContentProps) {
  // Live form-state window: empty string → fall back to persisted cfg
  // (matches the AudioPlayer start/end prop wiring further down). The
  // matchedSection check identifies which agent band (if any) the
  // current window corresponds to, so the metadata Row label can say
  // "Section #N" instead of always claiming #1. Uses the same shared
  // helper as AudioPlayer's per-band ✓ indicator — guarantees the two
  // surfaces never disagree.
  const liveStart =
    bestStart === "" ? (cfg.best_start_s ?? 0) : parseFloat(bestStart);
  const liveEnd =
    bestEnd === "" ? (cfg.best_end_s ?? 0) : parseFloat(bestEnd);
  const matchedSection = matchSectionByBounds(
    track.best_sections,
    liveStart,
    liveEnd,
  );
  // Form ↔ persisted divergence drives the amber "Unsaved changes"
  // badge next to the Save button. Compare strings to avoid float
  // formatting drift (the form holds raw input strings).
  const hasUnsavedChanges =
    bestStart !== (cfg.best_start_s?.toString() ?? "") ||
    bestEnd !== (cfg.best_end_s?.toString() ?? "") ||
    slotEveryN !== (cfg.slot_every_n_beats?.toString() ?? "8");

  // Live "would produce N slots" preview. Mirrors the backend PATCH
  // validator at `admin_music.py` so the user sees the same verdict
  // before hitting Save. When previewSlots === 0 the Save button
  // disables — never let the user submit a known-invalid combo only to
  // round-trip a 422.
  //
  // submitStart/submitEnd/submitN are the SHAPE that would be sent on
  // Save. Empty inputs become NaN (not the cfg fallback), so previewSlots
  // correctly reports 0 and Save disables — submitting an empty field
  // produces JSON `null` which 500s on int(None) at the backend.
  // (liveStart/liveEnd above keep the cfg fallback because they drive the
  // AudioPlayer's visible window and the matchedSection label, which
  // must remain valid for display purposes while the user edits.)
  const submitStart = bestStart === "" ? Number.NaN : parseFloat(bestStart);
  const submitEnd = bestEnd === "" ? Number.NaN : parseFloat(bestEnd);
  const submitN = slotEveryN === "" ? Number.NaN : parseInt(slotEveryN, 10);
  const liveN = Number.isFinite(submitN) ? submitN : (cfg.slot_every_n_beats ?? 8);
  const previewSlots = useMemo(
    () =>
      countSlotsClient(
        track.beat_timestamps_s ?? [],
        submitStart,
        submitEnd,
        submitN,
      ),
    [track.beat_timestamps_s, submitStart, submitEnd, submitN],
  );

  // F4 escape hatch: when the current (start, end, N) combo 0-slots and
  // the track HAS beats (analyzer ran), search the lower-N candidates for
  // one that would produce ≥1 slot. The user can click "Try N=k" inline
  // next to the amber badge and recover without manual guesswork. Common
  // hit: legacy rows saved before the slot-count validator, or tracks
  // whose re-analyzed beat density dropped under N=8.
  const suggestedN = useMemo(() => {
    if (previewSlots > 0) return null;
    const beats = track.beat_timestamps_s ?? [];
    if (beats.length === 0) return null;
    // Bounds must be finite to attempt a fix-by-N suggestion. If start/end
    // are blank/invalid, lowering N can't recover the situation.
    if (!Number.isFinite(submitStart) || !Number.isFinite(submitEnd)) return null;
    const currentN = Number.isFinite(submitN) ? submitN : NaN;
    for (const n of [4, 2, 1]) {
      if (n === currentN) continue;
      const slots = countSlotsClient(beats, submitStart, submitEnd, n);
      if (slots > 0) return { n, slots };
    }
    return null;
  }, [previewSlots, track.beat_timestamps_s, submitStart, submitEnd, submitN]);

  return (
    <>
      {/* Info card */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6 grid grid-cols-2 gap-x-8 gap-y-2 text-sm">
        <Row label="Artist" value={track.artist || "—"} />
        <Row label="Duration" value={track.duration_s ? `${track.duration_s.toFixed(1)}s` : "—"} />
        <Row label="Beats detected" value={String(track.beat_count)} />
        <Row
          label={matchedSection ? `Section #${matchedSection.rank}` : "Custom window"}
          value={
            Number.isFinite(liveStart) && Number.isFinite(liveEnd) && liveEnd > liveStart
              ? `${liveStart.toFixed(1)}s – ${liveEnd.toFixed(1)}s${matchedSection ? ` · ${matchedSection.label}` : ""}`
              : "—"
          }
        />
        <Row label="Slot every N beats" value={cfg.slot_every_n_beats?.toString() ?? "—"} />
        <Row
          label="Required clips"
          value={
            cfg.required_clips_min != null
              ? `${cfg.required_clips_min} – ${cfg.required_clips_max}`
              : "—"
          }
        />
        <div className="col-span-2">
          <span className="text-zinc-500">Source URL </span>
          <a
            href={track.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-violet-400 hover:text-violet-300 font-mono text-xs break-all"
          >
            {track.source_url}
          </a>
        </div>
        {track.error_detail && (
          <div className="col-span-2 text-red-400 text-xs break-words">
            Error: {track.error_detail}
          </div>
        )}
      </div>

      {/* Audio player + beat waveform. Require duration_s > 0 so the SVG
          math (x = start_s / duration) doesn't produce Infinity coords when
          beat detection succeeded but duration probing didn't. */}
      {track.analysis_status === "ready" &&
        track.duration_s != null && track.duration_s > 0 &&
        track.beat_timestamps_s && track.beat_timestamps_s.length > 0 && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
          <div className="flex items-center gap-3 mb-3">
            <h2 className="font-semibold text-sm text-zinc-400 uppercase tracking-wide flex-1">
              Audio · {track.beat_count} beats
            </h2>
            {track.section_version ? (
              <span
                className="text-xs text-zinc-400 font-mono"
                title="Prompt-version the agent scored this track under. Bump in song_sections.py forces re-section via the backfill script."
              >
                sections v{track.section_version}
              </span>
            ) : (
              <span
                className="text-xs text-amber-500"
                title="No agent sections stored — either the song_sections agent has not run, or the track was analyzed before the agent shipped."
              >
                no agent sections
              </span>
            )}
            <span
              className="text-xs text-zinc-400 font-mono"
              title="song_classifier coverage. Generative matching requires current-version AI labels."
            >
              {track.has_ai_labels
                ? `labels v${track.label_version ?? "?"}`
                : "no AI labels"}
            </span>
            <span
              className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                track.generative_matchable
                  ? "bg-emerald-500/15 text-emerald-400"
                  : "bg-zinc-800 text-zinc-500"
              }`}
              title={
                track.generative_matchable
                  ? "Eligible for generative auto-match (publish not required)."
                  : "Not eligible for generative auto-match — missing/stale AI labels or sections."
              }
            >
              {track.generative_matchable ? "matchable" : "not matchable"}
            </span>
          </div>
          <AudioPlayer
            trackId={id}
            beats={track.beat_timestamps_s}
            duration={track.duration_s ?? 0}
            // Explicit empty-string check, not `|| fallback`: parseFloat("0")
            // is 0 (falsy), which would silently fall through to the cfg
            // value for sections starting at 0.0s and desync the isSelected
            // indicator from the form input.
            start={bestStart === "" ? (cfg.best_start_s ?? 0) : parseFloat(bestStart)}
            end={bestEnd === "" ? (cfg.best_end_s ?? 0) : parseFloat(bestEnd)}
            sections={track.best_sections}
            slotEveryN={Number.isFinite(liveN) ? liveN : 8}
            onStartChange={(s) => setBestStart(s.toString())}
            onEndChange={(s) => setBestEnd(s.toString())}
          />
          {(!track.best_sections || track.best_sections.length === 0) && (
            <>
              <p className="text-xs text-zinc-500 mt-3 italic">
                The agent has not picked any sections for this track yet. Click <span className="text-zinc-300">Re-analyze beats</span>{" "}
                below — section analysis runs as part of the same task.
              </p>
              {/* When _run_song_sections caught a non-Refusal Exception on its
                  last attempt for this track, surface the truncated reason so
                  the operator can see WHY (Gemini transport, malformed JSON,
                  schema drift) before re-analyzing blindly. Cleared as soon
                  as the next analyze starts; NULL means "agent has not run"
                  rather than "agent ran clean." */}
              {track.section_error_detail && (
                <div className="mt-3">
                  <p className="text-xs text-zinc-500 mb-1">Last attempt failed:</p>
                  <pre className="text-xs font-mono text-zinc-500 bg-zinc-950 border border-zinc-800 rounded px-2 py-1.5 whitespace-pre-wrap break-words">
                    {track.section_error_detail}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* Config form */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
        <h2 className="font-semibold mb-4">Timing config</h2>
        <form onSubmit={handleSaveConfig} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <label className="block">
              <span className="text-xs text-zinc-400 mb-1 block">Best section start (s)</span>
              {/* step="any" intentionally: the song_sections agent stores
                  raw floats (e.g. 56.25) and step="0.1" rejected them with
                  a locale-formatted HTML5 popup. Bounds + slot-count are
                  enforced by the backend PATCH validator + previewSlots. */}
              <input
                type="number"
                step="any"
                min="0"
                value={bestStart}
                onChange={(e) => setBestStart(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
              />
            </label>
            <label className="block">
              <span className="text-xs text-zinc-400 mb-1 block">Best section end (s)</span>
              <input
                type="number"
                step="any"
                min="0"
                value={bestEnd}
                onChange={(e) => setBestEnd(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
              />
            </label>
          </div>
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">
              Slot every N beats (default: 8 = ~2 bars at 120 BPM)
            </span>
            <input
              type="number"
              step="1"
              min="1"
              max="32"
              value={slotEveryN}
              onChange={(e) => setSlotEveryN(e.target.value)}
              className="w-40 bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
            />
          </label>
          {saveMsg && (
            <p
              className={`text-sm ${saveMsg === "Saved." ? "text-green-400" : "text-red-400"}`}
            >
              {saveMsg}
            </p>
          )}
          <div className="flex items-center">
            <button
              type="submit"
              disabled={saving || previewSlots === 0}
              data-testid="save-config-btn"
              className="bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors"
            >
              {saving ? "Saving…" : "Save config"}
            </button>
            {/* Slot-count preview — mirrors backend admin_music.py PATCH
                validator. Green when the (window, N) combo would produce
                ≥1 slot; amber chip + Save disabled when it would 0-slot.
                The 0-slot variant uses a heavier chip treatment (border
                + ⚠ icon) so it doesn't visually equal the lighter
                "Unsaved changes" notice rendered below. */}
            {previewSlots > 0 ? (
              <span
                data-testid="slot-count-badge"
                role="status"
                aria-live="polite"
                className="ml-3 text-xs text-emerald-400 inline-flex items-center gap-1.5"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
                Would produce {previewSlots} slot{previewSlots === 1 ? "" : "s"}
              </span>
            ) : (
              <span
                data-testid="slot-count-badge"
                role="status"
                aria-live="polite"
                className="ml-3 text-xs font-medium text-amber-200 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-amber-500/15 border border-amber-500/40"
              >
                <span aria-hidden="true">⚠</span>
                Cannot save: 0 slots — lower N or widen window
              </span>
            )}
            {/* F4 escape: lower-N suggestion. Only shown when the user
                is stuck (0-slot) AND a lower N would unblock them.
                Clicking sets slotEveryN to the suggested value; the
                badge re-renders green and Save enables. */}
            {previewSlots === 0 && suggestedN && (
              <button
                type="button"
                data-testid="apply-suggested-n"
                onClick={() => setSlotEveryN(suggestedN.n.toString())}
                className="ml-2 text-xs font-medium text-amber-100 underline decoration-amber-400 underline-offset-2 hover:text-white"
              >
                Try N={suggestedN.n} ({suggestedN.slots} slot
                {suggestedN.slots === 1 ? "" : "s"})
              </button>
            )}
            {/* Amber, not red — "unsaved" is a state, not an error.
                Rendered subtler than the validation chip above so the
                two never visually collide. */}
            {hasUnsavedChanges && (
              <span className="ml-3 text-xs text-amber-400/70 inline-flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400/70 inline-block" />
                Unsaved changes
              </span>
            )}
          </div>
        </form>
      </div>

      {/* Lyrics section */}
      <LyricsConfigPanel
        kind="track"
        track={track}
        onTrackUpdated={setTrack as (t: MusicTrackDetail) => void}
        onDirtyChange={onLyricsDirtyChange}
      />

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        {track.analysis_status === "ready" && (
          <button
            onClick={handleCreateTemplate}
            disabled={creatingTemplate}
            className="text-sm font-semibold px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white transition-colors"
          >
            {creatingTemplate ? "Creating…" : "Create Template"}
          </button>
        )}

        <button
          onClick={handleTogglePublish}
          className={`text-sm font-semibold px-4 py-2 rounded-lg transition-colors ${
            track.published_at
              ? "bg-zinc-700 hover:bg-zinc-600 text-zinc-100"
              : "bg-green-700 hover:bg-green-600 text-white"
          }`}
        >
          {track.published_at ? "Unpublish" : "Publish to gallery"}
        </button>

        <button
          onClick={handleReanalyze}
          disabled={reanalyzing || track.analysis_status === "analyzing"}
          className="text-sm font-semibold px-4 py-2 rounded-lg bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40 transition-colors"
        >
          {reanalyzing ? "Re-analyzing…" : "Re-analyze beats"}
        </button>

        {!track.archived_at && (
          <button
            onClick={handleArchive}
            className="text-sm font-semibold px-4 py-2 rounded-lg bg-red-900 hover:bg-red-800 text-red-200 transition-colors ml-auto"
          >
            Archive track
          </button>
        )}
      </div>
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-zinc-500">{label} </span>
      <span className="text-zinc-100">{value}</span>
    </div>
  );
}
