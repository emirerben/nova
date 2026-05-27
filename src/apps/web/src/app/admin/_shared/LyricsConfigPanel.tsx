"use client";

import { useEffect, useState } from "react";
import {
  adminExtractLyrics,
  adminForceLrclibId,
  adminGetMusicTrack,
  adminUpdateMusicTrack,
  type LyricsCache,
  type LyricsConfig,
  type LyricsDiagnostic,
  type LyricsStatus,
  type MusicTrackDetail,
} from "@/lib/music-api";
import {
  adminUpdateTemplateLyricsConfig,
  type AdminTemplate,
} from "@/lib/admin-api";

const STATUS_COLORS: Record<LyricsStatus, string> = {
  pending: "bg-zinc-700 text-zinc-300",
  extracting: "bg-blue-900 text-blue-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
  unavailable: "bg-amber-900 text-amber-300",
  // Added 2026-05-27 (Beauty And A Beat PR). Orange — operator action required.
  needs_manual_lyrics: "bg-orange-900 text-orange-300",
};

/**
 * When the LRCLIB-recording-duration vs uploaded-audio-duration delta crosses
 * this threshold, the UI renders a warning banner telling the operator the
 * matched row may be a different recording (extended cut, radio edit, etc.).
 * Aligned with the soft-signal threshold in
 * `lrclib_client._FUZZY_DURATION_DELTA_WARN_S`.
 */
const DURATION_MISMATCH_WARN_S = 5;

function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  const sign = s < 0 ? "-" : "";
  const abs = Math.abs(s);
  const m = Math.floor(abs / 60);
  const sec = Math.floor(abs % 60);
  return `${sign}${m}:${String(sec).padStart(2, "0")}`;
}

const STYLE_OPTIONS: { value: LyricsConfig["style"]; label: string }[] = [
  { value: "karaoke", label: "Karaoke (sing-along highlight)" },
  { value: "per-word-pop", label: "Per-word pop-in (TikTok style)" },
  { value: "line", label: "Line (fade in/out, full-line)" },
];

const POSITION_OPTIONS: { value: string; label: string }[] = [
  { value: "bottom", label: "Bottom" },
  { value: "center", label: "Center" },
  { value: "top", label: "Top" },
];

function defaultConfig(): LyricsConfig {
  return {
    enabled: false,
    style: "karaoke",
    position: "bottom",
    text_color: "#FFFFFF",
    highlight_color: "#FFFF00",
    font_style: "sans",
    text_size: "medium",
    outline_px: 2,
  };
}

function coerceConfig(raw: unknown): LyricsConfig {
  if (raw && typeof raw === "object") {
    return { ...defaultConfig(), ...(raw as Partial<LyricsConfig>) };
  }
  return defaultConfig();
}

// ─── Track variant ────────────────────────────────────────────────────────────

type TrackProps = {
  kind: "track";
  track: MusicTrackDetail;
  onTrackUpdated: (t: MusicTrackDetail) => void;
  /**
   * Called when the form's local state diverges from what's persisted on the
   * track. The parent page uses this to warn before the user kicks off a
   * Create Template action with unsaved lyrics edits — see the "unsaved
   * checkbox footgun" callout in the plan.
   */
  onDirtyChange?: (isDirty: boolean, currentCfg: LyricsConfig) => void;
};

// ─── Template variant ─────────────────────────────────────────────────────────

type TemplateProps = {
  kind: "template";
  template: AdminTemplate;
  /**
   * Linked track (lyrics_status, lyrics_cached, etc.) — fetched by the
   * parent so the panel can render the cached-lines preview and status badge
   * exactly like the track variant.
   */
  track: MusicTrackDetail;
  onTemplateUpdated: (t: AdminTemplate) => void;
};

type Props = TrackProps | TemplateProps;

export default function LyricsConfigPanel(props: Props) {
  // For the template variant, the active config is the override when set,
  // otherwise the linked track's config — matches the orchestrator's
  // `is not None` resolution. NEVER use `||` here; `{}` is a legit value.
  const initial: LyricsConfig =
    props.kind === "track"
      ? coerceConfig(props.track.track_config?.lyrics_config)
      : props.template.lyrics_config !== null
        ? coerceConfig(props.template.lyrics_config)
        : coerceConfig(props.track.track_config?.lyrics_config);

  const [cfg, setCfg] = useState<LyricsConfig>(initial);
  const [saving, setSaving] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [previewExpanded, setPreviewExpanded] = useState(false);
  const [draftPreviewExpanded, setDraftPreviewExpanded] = useState(false);
  // Beauty And A Beat (2026-05-27): paste-ID input for the needs_manual_lyrics
  // recovery flow. Accepts either a numeric LRCLIB row ID or an lrclib.net URL.
  const [forceIdInput, setForceIdInput] = useState("");
  const [forcing, setForcing] = useState(false);

  const status = props.track.lyrics_status;
  const cache = props.track.lyrics_cached;

  const persisted: LyricsConfig | null =
    props.kind === "track"
      ? (props.track.track_config?.lyrics_config ?? null)
      : (props.template.lyrics_config as LyricsConfig | null);

  const isCustom =
    props.kind === "template" && props.template.lyrics_config !== null;

  // Dirty-tracking: ping the parent whenever the local form diverges from
  // what's on the server. Only relevant for the track variant (the music
  // page uses this to gate Create Template).
  useEffect(() => {
    if (props.kind !== "track" || !props.onDirtyChange) return;
    const dirty = JSON.stringify(cfg) !== JSON.stringify(persisted ?? defaultConfig());
    props.onDirtyChange(dirty, cfg);
  }, [cfg, persisted, props]);

  async function handleSave() {
    setSaving(true);
    setMsg(null);
    try {
      if (props.kind === "track") {
        const updated = await adminUpdateMusicTrack(props.track.id, {
          track_config: {
            ...(props.track.track_config ?? {}),
            lyrics_config: cfg,
          },
        });
        props.onTrackUpdated(updated);
      } else {
        const updated = await adminUpdateTemplateLyricsConfig(
          props.template.id,
          cfg as unknown as Record<string, unknown>,
        );
        props.onTemplateUpdated(updated);
      }
      setMsg("Saved.");
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleResetToTrack() {
    if (props.kind !== "template") return;
    setSaving(true);
    setMsg(null);
    try {
      const updated = await adminUpdateTemplateLyricsConfig(
        props.template.id,
        null,
      );
      props.onTemplateUpdated(updated);
      setCfg(coerceConfig(props.track.track_config?.lyrics_config));
      setMsg("Reset to track config.");
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleExtract() {
    if (props.kind !== "track") return;
    setExtracting(true);
    setMsg(null);
    try {
      await adminExtractLyrics(props.track.id);
      const start = Date.now();
      while (Date.now() - start < 120_000) {
        await new Promise((r) => setTimeout(r, 2500));
        const fresh = await adminGetMusicTrack(props.track.id);
        props.onTrackUpdated(fresh);
        if (fresh.lyrics_status !== "extracting") return;
      }
      setMsg("Extraction is still running — refresh in a minute.");
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Extract failed");
    } finally {
      setExtracting(false);
    }
  }

  /**
   * Beauty And A Beat (2026-05-27): admin pastes an LRCLIB row ID/URL,
   * server validates + persists `forced_lrclib_id` to
   * `track_config.lyrics_config`, bumps `lyrics_extraction_version`, and
   * dispatches re-extraction. The agent fetches /api/get/{id} directly,
   * skipping title/artist search.
   */
  async function handleForceLrclibId() {
    if (props.kind !== "track") return;
    const trimmed = forceIdInput.trim();
    if (!trimmed) {
      setMsg("Paste an LRCLIB row ID or lrclib.net URL first.");
      return;
    }
    setForcing(true);
    setMsg(null);
    try {
      await adminForceLrclibId(props.track.id, trimmed);
      setForceIdInput("");
      // Poll the same way Re-extract does. Same 2-minute ceiling.
      const start = Date.now();
      while (Date.now() - start < 120_000) {
        await new Promise((r) => setTimeout(r, 2500));
        const fresh = await adminGetMusicTrack(props.track.id);
        props.onTrackUpdated(fresh);
        if (fresh.lyrics_status !== "extracting") return;
      }
      setMsg("Extraction is still running — refresh in a minute.");
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Force LRCLIB ID failed");
    } finally {
      setForcing(false);
    }
  }

  return (
    <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-semibold">Lyrics</h2>
        <span
          className={`text-xs font-semibold px-2 py-1 rounded-full ${
            STATUS_COLORS[status] ?? STATUS_COLORS.pending
          }`}
        >
          {status}
        </span>
      </div>

      {/* Template variant: inheritance state strip */}
      {props.kind === "template" && (
        <div
          className={`text-xs mb-4 px-3 py-2 rounded-lg border ${
            isCustom
              ? "bg-amber-950/40 border-amber-800 text-amber-200"
              : "bg-zinc-950 border-zinc-800 text-zinc-400"
          }`}
        >
          {isCustom ? (
            <div className="flex items-center justify-between gap-3">
              <span>
                Custom to this template. Track edits no longer affect this
                template until you reset.
              </span>
              <button
                onClick={handleResetToTrack}
                disabled={saving}
                className="text-xs font-semibold px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 text-zinc-100"
              >
                Reset to inherit from track
              </button>
            </div>
          ) : (
            <span>
              Inherits from the linked music track. Edits to the track flow
              through here. Save below to lock in a per-template override.
            </span>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm mb-4">
        <div>
          <span className="text-zinc-500">Source </span>
          <span className="text-zinc-100">{props.track.lyrics_source ?? "—"}</span>
        </div>
        <div>
          <span className="text-zinc-500">Language </span>
          <span className="text-zinc-100">{cache?.language || "—"}</span>
        </div>
        <div>
          <span className="text-zinc-500">Lines </span>
          <span className="text-zinc-100">{cache?.lines?.length ?? 0}</span>
        </div>
        <div>
          <span className="text-zinc-500">Confidence </span>
          <span className="text-zinc-100">
            {cache?.confidence != null ? cache.confidence.toFixed(2) : "—"}
          </span>
        </div>
        {cache?.genius_url && (
          <div className="col-span-2">
            <span className="text-zinc-500">Matched on Genius </span>
            <a
              href={cache.genius_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-violet-400 hover:text-violet-300 text-xs break-all"
            >
              {cache.track_title_matched} — {cache.artist_matched}
            </a>
          </div>
        )}
        {props.track.lyrics_error_detail && (
          <div className="col-span-2 text-red-400 text-xs break-words">
            {props.track.lyrics_error_detail}
          </div>
        )}
      </div>

      {/* Duration-mismatch warning banner (added 2026-05-27). Surfaces the
          delta between LRCLIB's recording length and the uploaded audio so
          the operator knows when a force-ID has likely matched the wrong
          recording (extended cut, radio edit). Non-blocking. */}
      {props.track.lyrics_diagnostic?.duration_delta_s != null &&
        Math.abs(props.track.lyrics_diagnostic.duration_delta_s) >=
          DURATION_MISMATCH_WARN_S && (
          <div className="bg-amber-950/40 border border-amber-800 rounded-lg p-3 mb-4 text-xs text-amber-200">
            <div className="font-semibold mb-1">
              ⚠ LRCLIB recording duration differs from your audio
            </div>
            <div>
              Δ {props.track.lyrics_diagnostic.duration_delta_s.toFixed(1)}s
              {props.track.duration_s != null && (
                <>
                  {" "}
                  (audio {formatSeconds(props.track.duration_s)})
                </>
              )}
              . This may be a different recording — verify the lyrics line up
              before enabling.
            </div>
          </div>
        )}

      {/* needs_manual_lyrics recovery block (added 2026-05-27 Beauty And A Beat).
          Shows the diagnostic of WHY LRCLIB lookup missed, then a paste-ID
          input for the recovery path. */}
      {props.kind === "track" && status === "needs_manual_lyrics" && (
        <div className="bg-zinc-950 border border-orange-800/40 rounded-lg p-3 mb-4 space-y-3">
          <div>
            <div className="text-orange-200 text-sm font-semibold mb-1">
              LRCLIB lookup didn’t find a matching row
            </div>
            <div className="text-xs text-zinc-400">
              Find the right row at{" "}
              <a
                href="https://lrclib.net/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-violet-400 hover:text-violet-300"
              >
                lrclib.net
              </a>{" "}
              and paste the row’s numeric ID or full URL below to recover.
            </div>
          </div>

          {props.track.lyrics_diagnostic && (
            <DiagnosticBlock diagnostic={props.track.lyrics_diagnostic} />
          )}

          <div className="flex gap-2">
            <input
              type="text"
              value={forceIdInput}
              onChange={(e) => setForceIdInput(e.target.value)}
              placeholder="https://lrclib.net/lyrics/12345 or 12345"
              disabled={forcing || status !== "needs_manual_lyrics"}
              className="flex-1 bg-zinc-900 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500 disabled:opacity-40"
            />
            <button
              onClick={handleForceLrclibId}
              disabled={forcing || !forceIdInput.trim()}
              className="bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors whitespace-nowrap"
            >
              {forcing ? "Re-extracting…" : "Force LRCLIB ID"}
            </button>
          </div>

          {/* Whisper-only draft kept for admin reference — never burned into
              renders. Render under a clear "draft only" label so an operator
              cross-checking timestamps against lrclib.net candidates doesn't
              mistake it for production output. */}
          {props.track.lyrics_whisper_draft?.lines &&
            props.track.lyrics_whisper_draft.lines.length > 0 && (
              <DraftTranscriptBlock
                draft={props.track.lyrics_whisper_draft}
                expanded={draftPreviewExpanded}
                onToggle={() => setDraftPreviewExpanded(!draftPreviewExpanded)}
              />
            )}
        </div>
      )}

      {/* Cached preview */}
      {cache?.lines && cache.lines.length > 0 && (
        <div className="bg-zinc-950 rounded-lg border border-zinc-800 p-3 mb-4">
          <button
            onClick={() => setPreviewExpanded(!previewExpanded)}
            className="text-xs text-zinc-400 hover:text-zinc-200 mb-2"
          >
            {previewExpanded
              ? "▼ Hide preview"
              : `▶ Preview (${cache.lines.length} lines)`}
          </button>
          {previewExpanded && (
            <ol className="text-xs text-zinc-300 space-y-1 max-h-64 overflow-y-auto font-mono">
              {cache.lines.map((line, i) => (
                <li key={i}>
                  <span className="text-zinc-600 tabular-nums">
                    {line.start_s.toFixed(2)}s
                  </span>{" "}
                  {line.text}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {/* Visual config form */}
      <div className="space-y-3 mb-4">
        <label
          className={`flex items-center gap-2 text-sm ${
            status !== "ready" ? "opacity-50 cursor-not-allowed" : ""
          }`}
          title={
            status !== "ready"
              ? `Status must be 'ready' to enable lyrics (currently ${status})`
              : undefined
          }
        >
          <input
            type="checkbox"
            checked={cfg.enabled}
            // Beauty And A Beat (2026-05-27): can't enable lyrics on a track
            // that isn't ready. FE-side gate mirrors the backend's
            // update_music_track PATCH gate (which rejects with 422 if you
            // bypass this).
            disabled={status !== "ready"}
            onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
            className="rounded"
          />
          <span>
            {props.kind === "track"
              ? "Enable lyrics in music jobs using this track"
              : "Enable lyrics for jobs rendered from this template"}
          </span>
        </label>

        <div className="grid grid-cols-2 gap-4">
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">Animation style</span>
            <select
              value={cfg.style}
              onChange={(e) =>
                setCfg({ ...cfg, style: e.target.value as LyricsConfig["style"] })
              }
              className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
            >
              {STYLE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">Position</span>
            <select
              value={cfg.position ?? "bottom"}
              onChange={(e) => setCfg({ ...cfg, position: e.target.value })}
              className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
            >
              {POSITION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">Text color</span>
            <input
              type="color"
              value={cfg.text_color ?? "#FFFFFF"}
              onChange={(e) => setCfg({ ...cfg, text_color: e.target.value })}
              className="w-full h-9 bg-zinc-800 border border-zinc-600 rounded-lg"
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">
              Highlight color (karaoke only)
            </span>
            <input
              type="color"
              value={cfg.highlight_color ?? "#FFFF00"}
              onChange={(e) =>
                setCfg({ ...cfg, highlight_color: e.target.value })
              }
              disabled={cfg.style !== "karaoke"}
              className="w-full h-9 bg-zinc-800 border border-zinc-600 rounded-lg disabled:opacity-40"
            />
          </label>
        </div>
      </div>

      {msg && (
        <p
          className={`text-sm mb-3 ${
            msg.startsWith("Sav") || msg.startsWith("Reset")
              ? "text-green-400"
              : "text-red-400"
          }`}
        >
          {msg}
        </p>
      )}

      <div className="flex flex-wrap gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors"
        >
          {saving
            ? "Saving…"
            : props.kind === "track"
              ? "Save lyrics config"
              : isCustom
                ? "Save override"
                : "Customize for this template"}
        </button>
        {props.kind === "track" && (
          <button
            onClick={handleExtract}
            disabled={extracting || status === "extracting" || forcing}
            className="bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40 text-zinc-100 text-sm font-semibold px-4 py-2 rounded-lg transition-colors"
          >
            {extracting || status === "extracting"
              ? "Extracting…"
              : "Re-extract lyrics"}
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * Compact, scannable rendering of the LRCLIB-lookup diagnostic. Shows
 * exactly which title/artist the agent sent and which pass (get / search /
 * forced-id) yielded the terminal state. Reads loosely — if a future
 * backend adds a field, we render what we know and ignore the rest.
 */
function DiagnosticBlock({ diagnostic }: { diagnostic: LyricsDiagnostic }) {
  const q = diagnostic.query;
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded p-2 text-xs font-mono space-y-0.5">
      <div className="text-zinc-500 mb-1 font-sans not-italic">Diagnostic</div>
      <div>
        <span className="text-zinc-500">queried </span>
        <span className="text-zinc-100">{q.title || "—"}</span>
        <span className="text-zinc-500"> by </span>
        <span className="text-zinc-100">{q.artist || "—"}</span>
        {q.duration_s != null && (
          <>
            <span className="text-zinc-500"> · duration </span>
            <span className="text-zinc-100">{formatSeconds(q.duration_s)}</span>
          </>
        )}
      </div>
      {q.forced_lrclib_id != null && (
        <div>
          <span className="text-zinc-500">forced LRCLIB id </span>
          <span className="text-zinc-100">{q.forced_lrclib_id}</span>
        </div>
      )}
      <div>
        <span className="text-zinc-500">/api/get </span>
        <span className="text-zinc-100">{diagnostic.get_status}</span>
        {diagnostic.search_status !== "skipped" && (
          <>
            <span className="text-zinc-500"> · /api/search </span>
            <span className="text-zinc-100">{diagnostic.search_status}</span>
            {diagnostic.search_top_score != null && (
              <>
                <span className="text-zinc-500"> (top </span>
                <span className="text-zinc-100">
                  {diagnostic.search_top_score.toFixed(2)}
                </span>
                <span className="text-zinc-500">)</span>
              </>
            )}
          </>
        )}
      </div>
      <div>
        <span className="text-zinc-500">fallback path </span>
        <span className="text-zinc-100">{diagnostic.fallback_path}</span>
        {diagnostic.lrclib_id_matched != null && (
          <>
            <span className="text-zinc-500"> · matched id </span>
            <span className="text-zinc-100">
              {diagnostic.lrclib_id_matched}
            </span>
          </>
        )}
      </div>
      {diagnostic.lrclib_error && (
        <div className="text-red-400 break-words">
          <span className="text-zinc-500">error </span>
          {diagnostic.lrclib_error}
        </div>
      )}
    </div>
  );
}

/**
 * Whisper-only draft transcript shown under a clear "not used in renders"
 * label. The operator cross-checks these timestamps against lrclib.net
 * candidates to pick the right row for the force-ID input.
 */
function DraftTranscriptBlock({
  draft,
  expanded,
  onToggle,
}: {
  draft: LyricsCache;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded p-2 text-xs">
      <button
        onClick={onToggle}
        className="text-zinc-400 hover:text-zinc-200 font-sans"
      >
        {expanded
          ? "▼ Hide draft transcription"
          : `▶ Draft transcription — ${draft.lines.length} lines (Whisper-only, NOT used in renders)`}
      </button>
      {expanded && (
        <ol className="text-zinc-300 space-y-1 max-h-64 overflow-y-auto font-mono mt-2">
          {draft.lines.map((line, i) => (
            <li key={i}>
              <span className="text-zinc-600 tabular-nums">
                {line.start_s.toFixed(2)}s
              </span>{" "}
              {line.text}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
