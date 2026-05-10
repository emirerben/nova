"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AdminTemplate,
  type LatestTestJob,
  type RecipeVersionItem,
  type TemplateMetrics,
  adminCreateTestJob,
  adminGetLatestTestJob,
  adminGetMetrics,
  adminGetPresignedUpload,
  adminGetRecipe,
  adminGetRecipeHistory,
  adminGetTemplate,
  adminReanalyzeTemplate,
  adminSaveRecipe,
  adminUpdateTemplate,
} from "@/lib/admin-api";
import {
  type TemplateJobStatusResponse,
  getTemplateJobStatus,
  getTemplatePlaybackUrl,
  normaliseMimeType,
  uploadFileToGcs,
  uploadTemplatePhoto,
} from "@/lib/api";
import { useFileUpload } from "@/hooks/useFileUpload";
import { useJobPoller } from "@/hooks/useJobPoller";
import { EditorTab } from "./components/EditorTab";
import { MusicTab } from "./components/MusicTab";

// ── Types ──────────────────────────────────────────────────────────────────────

type TabId = "recipe" | "editor" | "test" | "music" | "settings";

const ALL_TABS: { id: TabId; label: string }[] = [
  { id: "recipe", label: "Recipe" },
  { id: "editor", label: "Editor" },
  { id: "test", label: "Test" },
  { id: "music", label: "Music" },
  { id: "settings", label: "Settings" },
];

function getVisibleTabs(templateType: string): { id: TabId; label: string }[] {
  if (templateType === "music_parent") {
    return ALL_TABS; // all 5 tabs
  }
  // standard, music_child, and audio_only: no Music tab
  return ALL_TABS.filter((t) => t.id !== "music");
}

const TERMINAL_STATUSES = new Set(["template_ready", "processing_failed"]);

// ── Page ───────────────────────────────────────────────────────────────────────

export default function TemplateDetailPage() {
  const { id } = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();

  const activeTab = (searchParams.get("tab") as TabId) || "recipe";

  const [template, setTemplate] = useState<AdminTemplate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null);
  const [latestTestJob, setLatestTestJob] = useState<LatestTestJob | null>(null);

  // Fetch template data
  useEffect(() => {
    adminGetTemplate(id)
      .then((t) => {
        setTemplate(t);
        // Skip playback URL for audio-only templates (no video file)
        if (t.template_type !== "audio_only" && t.gcs_path) {
          getTemplatePlaybackUrl(id)
            .then((r) => setPlaybackUrl(r.url))
            .catch(() => {}); // playback may not be ready yet
        }
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [id]);

  // Fetch latest test job for editor video sync
  useEffect(() => {
    adminGetLatestTestJob(id).then(setLatestTestJob).catch(() => {});
  }, [id]);

  const handleTestJobComplete = useCallback((job: LatestTestJob) => {
    setLatestTestJob(job);
  }, []);

  const setTab = useCallback(
    (tab: TabId) => {
      router.replace(`/admin/templates/${id}?tab=${tab}`);
    },
    [id, router],
  );

  const refreshTemplate = useCallback(async () => {
    try {
      const t = await adminGetTemplate(id);
      setTemplate(t);
    } catch {}
  }, [id]);

  // ── Actions ──────────────────────────────────────────────────────────────

  const handlePublish = useCallback(async () => {
    if (!template) return;
    if (!confirm("Publish this template? It will appear in the public gallery.")) return;
    setActionLoading(true);
    try {
      const updated = await adminUpdateTemplate(id, { publish: true });
      setTemplate(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Publish failed");
    } finally {
      setActionLoading(false);
    }
  }, [id, template]);

  const handleArchive = useCallback(async () => {
    if (!template) return;
    if (!confirm("Archive this template? It will be hidden from the public gallery.")) return;
    setActionLoading(true);
    try {
      const updated = await adminUpdateTemplate(id, { archive: true });
      setTemplate(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Archive failed");
    } finally {
      setActionLoading(false);
    }
  }, [id, template]);

  const handleReanalyze = useCallback(async () => {
    if (
      !confirm(
        "Re-run Gemini analysis?\n\n" +
          "WARNING: this OVERWRITES the saved recipe with a fresh analysis. " +
          "Any text-size, position, or other manual edits you've made will be LOST. " +
          "(The previous version is kept in history but is not used at render time.)\n\n" +
          "Takes a few minutes. Continue?",
      )
    ) {
      return;
    }
    setActionLoading(true);
    try {
      const updated = await adminReanalyzeTemplate(id);
      setTemplate(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Reanalyze failed");
    } finally {
      setActionLoading(false);
    }
  }, [id]);

  // ── Render ───────────────────────────────────────────────────────────────

  if (loading) return <PageSkeleton />;
  if (error || !template) {
    return (
      <div className="p-8">
        <p className="text-red-400">{error ?? "Template not found"}</p>
        <button onClick={() => router.push("/admin")} className="mt-4 text-sm text-zinc-400 hover:text-white">
          Back to dashboard
        </button>
      </div>
    );
  }

  const visibleTabs = getVisibleTabs(template.template_type ?? "standard");
  const resolvedTab = visibleTabs.some((t) => t.id === activeTab) ? activeTab : "recipe";

  return (
    <div className="flex flex-col min-h-screen">
      {/* Header */}
      <div className="border-b border-zinc-800 px-6 py-4">
        {template.template_type === "music_child" && template.parent_template_id ? (
          <button
            onClick={() => router.push(`/admin/templates/${template.parent_template_id}?tab=music`)}
            className="text-sm text-zinc-500 hover:text-zinc-300 mb-2 block"
          >
            ← Back to parent template
          </button>
        ) : (
          <button onClick={() => router.push("/admin")} className="text-sm text-zinc-500 hover:text-zinc-300 mb-2 block">
            ← Back to dashboard
          </button>
        )}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-white">{template.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={template.analysis_status} />
              {template.template_type === "music_child" && (
                <span className="text-xs bg-purple-900/40 text-purple-400 px-2 py-0.5 rounded">Music Variant</span>
              )}
              {template.template_type === "music_parent" && (
                <span className="text-xs bg-blue-900/40 text-blue-400 px-2 py-0.5 rounded">Music Parent</span>
              )}
              {template.template_type === "audio_only" && (
                <span className="text-xs bg-amber-900/40 text-amber-400 px-2 py-0.5 rounded">Audio Only</span>
              )}
              {template.published_at && !template.archived_at && (
                <span className="text-xs bg-green-900/40 text-green-400 px-2 py-0.5 rounded">Published</span>
              )}
              {template.archived_at && (
                <span className="text-xs bg-zinc-700 text-zinc-400 px-2 py-0.5 rounded">Archived</span>
              )}
              <span className="text-xs text-zinc-500">
                Created {new Date(template.created_at).toLocaleDateString()}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-zinc-800 px-6">
        <div className="flex gap-1">
          {visibleTabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setTab(tab.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                resolvedTab === tab.id
                  ? "border-white text-white"
                  : "border-transparent text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-auto px-6 py-6">
        {resolvedTab === "recipe" && (
          <RecipeTab
            template={template}
            playbackUrl={playbackUrl}
            onRefresh={refreshTemplate}
          />
        )}
        {resolvedTab === "editor" && (
          <EditorTab
            template={template}
            latestTestJob={latestTestJob}
            onTestJobComplete={handleTestJobComplete}
          />
        )}
        {resolvedTab === "test" && (
          <TestTab
            template={template}
            playbackUrl={playbackUrl}
            onJobComplete={handleTestJobComplete}
          />
        )}
        {resolvedTab === "music" && template.template_type === "music_parent" && (
          <MusicTab template={template} />
        )}
        {resolvedTab === "settings" && (
          <SettingsTab template={template} onSave={setTemplate} />
        )}
      </div>

      {/* Sticky action bar */}
      <div className="sticky bottom-0 border-t border-zinc-800 bg-zinc-950/95 backdrop-blur px-6 py-3 flex items-center gap-3">
        <button
          onClick={handleReanalyze}
          disabled={actionLoading || template.analysis_status === "analyzing"}
          className="px-4 py-2 text-sm bg-zinc-800 hover:bg-zinc-700 text-white rounded disabled:opacity-50"
        >
          {template.analysis_status === "analyzing" ? "Analyzing..." : "Reanalyze"}
        </button>
        {!template.published_at || template.archived_at ? (
          <button
            onClick={handlePublish}
            disabled={actionLoading || template.analysis_status !== "ready"}
            className="px-4 py-2 text-sm bg-green-700 hover:bg-green-600 text-white rounded disabled:opacity-50"
          >
            Publish
          </button>
        ) : (
          <button
            onClick={handleArchive}
            disabled={actionLoading}
            className="px-4 py-2 text-sm bg-red-900/60 hover:bg-red-800 text-white rounded disabled:opacity-50"
          >
            Archive
          </button>
        )}
      </div>
    </div>
  );
}

// ── Recipe Tab ─────────────────────────────────────────────────────────────────

function RecipeTab({
  template,
  playbackUrl,
  onRefresh,
}: {
  template: AdminTemplate;
  playbackUrl: string | null;
  onRefresh: () => void;
}) {
  const [recipeHistory, setRecipeHistory] = useState<RecipeVersionItem[]>([]);

  useEffect(() => {
    adminGetRecipeHistory(template.id).then((r) => setRecipeHistory(r.versions)).catch(() => {});
  }, [template.id]);

  // Poll if still analyzing
  const poller = useJobPoller<AdminTemplate>(
    template.analysis_status === "analyzing" ? template.id : null,
    {
      fetchStatus: adminGetTemplate,
      isTerminal: (t) => t.analysis_status !== "analyzing",
      intervalMs: 3000,
    },
  );

  useEffect(() => {
    if (poller.data && poller.data.analysis_status !== "analyzing") {
      onRefresh();
    }
  }, [poller.data, onRefresh]);

  const latestRecipe = recipeHistory.length > 0 ? recipeHistory[0] : null;

  const isAudioOnly = template.template_type === "audio_only";

  if (template.analysis_status === "analyzing") {
    return (
      <div className="flex gap-6">
        {playbackUrl && <VideoPlayer url={playbackUrl} />}
        {isAudioOnly && !playbackUrl && <AudioOnlyPlaceholder />}
        <div className="flex-1 flex flex-col items-center justify-center gap-4 py-12">
          <div className="w-8 h-8 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
          <p className="text-zinc-400 text-sm">Analyzing template with Gemini...</p>
          <p className="text-zinc-600 text-xs">This typically takes 2-5 minutes</p>
        </div>
      </div>
    );
  }

  if (template.analysis_status === "failed") {
    return (
      <div className="flex gap-6">
        {playbackUrl && <VideoPlayer url={playbackUrl} />}
        {isAudioOnly && !playbackUrl && <AudioOnlyPlaceholder />}
        <div className="flex-1 py-12 text-center">
          <p className="text-red-400 mb-2">Analysis failed</p>
          <p className="text-zinc-500 text-sm">Try clicking &ldquo;Reanalyze&rdquo; in the action bar below.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex gap-6">
        {playbackUrl && <VideoPlayer url={playbackUrl} />}
        {isAudioOnly && !playbackUrl && <AudioOnlyPlaceholder />}
        <div className="flex-1 space-y-4">
          {latestRecipe && (
            <div className="grid grid-cols-3 gap-3">
              <MetricCard label="Slots" value={latestRecipe.slot_count} />
              <MetricCard label="Duration" value={`${latestRecipe.total_duration_s.toFixed(1)}s`} />
              <MetricCard label="Version" value={`#${recipeHistory.length}`} />
            </div>
          )}
        </div>
      </div>

      {/* Recipe history */}
      {recipeHistory.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-zinc-400 mb-3">Recipe History</h3>
          <div className="border border-zinc-800 rounded overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900">
                <tr>
                  <th className="text-left px-4 py-2 text-zinc-500 font-medium">#</th>
                  <th className="text-left px-4 py-2 text-zinc-500 font-medium">Trigger</th>
                  <th className="text-left px-4 py-2 text-zinc-500 font-medium">Slots</th>
                  <th className="text-left px-4 py-2 text-zinc-500 font-medium">Duration</th>
                  <th className="text-left px-4 py-2 text-zinc-500 font-medium">Date</th>
                </tr>
              </thead>
              <tbody>
                {recipeHistory.map((v, i) => (
                  <tr key={v.id} className="border-t border-zinc-800">
                    <td className="px-4 py-2 text-zinc-300">{recipeHistory.length - i}</td>
                    <td className="px-4 py-2">
                      <span className={`text-xs px-2 py-0.5 rounded ${
                        v.trigger === "initial_analysis" ? "bg-blue-900/40 text-blue-400" :
                        v.trigger === "reanalysis" ? "bg-amber-900/40 text-amber-400" :
                        "bg-zinc-700 text-zinc-300"
                      }`}>
                        {v.trigger.replace("_", " ")}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-zinc-300">{v.slot_count}</td>
                    <td className="px-4 py-2 text-zinc-300">{v.total_duration_s.toFixed(1)}s</td>
                    <td className="px-4 py-2 text-zinc-500">{new Date(v.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Test Tab ───────────────────────────────────────────────────────────────────

function TestTab({
  template,
  playbackUrl,
  onJobComplete,
}: {
  template: AdminTemplate;
  playbackUrl: string | null;
  onJobComplete?: (job: LatestTestJob) => void;
}) {
  const [testJobId, setTestJobId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [rerolling, setRerolling] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<TemplateMetrics | null>(null);
  const [latestJob, setLatestJob] = useState<LatestTestJob | null>(null);
  // Recipe slot summary — drives slot-bound upload for mixed-media templates.
  const [recipeSlots, setRecipeSlots] = useState<Array<{
    position: number;
    target_duration_s: number;
    media_type: "video" | "photo";
  }>>([]);

  // Templates with an intro slot split the upload into Part 1 (a single
  // pinned clip or photo for slot 1) and Part 2 (action clips for the rest).
  // Toggled per template via the Settings tab; persists in
  // recipe_cached.has_intro_slot.
  const hasIntroSlot = template.has_intro_slot ?? false;

  // Load latest test job on mount so previous results are visible
  useEffect(() => {
    adminGetLatestTestJob(template.id).then((job) => {
      if (job) setLatestJob(job);
    }).catch(() => {});
  }, [template.id]);

  // Load recipe slots so we can render slot-bound UI when any slot wants a photo
  useEffect(() => {
    adminGetRecipe(template.id).then((r) => {
      const raw = (r.recipe as { slots?: unknown }).slots;
      if (!Array.isArray(raw)) return;
      const parsed = raw.map((s, i) => {
        const slot = s as Record<string, unknown>;
        return {
          position: typeof slot.position === "number" ? slot.position : i + 1,
          target_duration_s:
            typeof slot.target_duration_s === "number" ? slot.target_duration_s : 5.0,
          media_type:
            (slot.media_type === "photo" ? "photo" : "video") as "video" | "photo",
        };
      });
      parsed.sort((a, b) => a.position - b.position);
      setRecipeSlots(parsed);
    }).catch(() => {});
  }, [template.id]);

  const isSlotBound = recipeSlots.some((s) => s.media_type === "photo");

  // File upload for test clips
  const upload = useFileUpload({
    getPresignedUrl: async (file) => {
      return adminGetPresignedUpload(file.name, file.type || "video/mp4");
    },
  });

  // Separate uploader for the face/intro clip — single file, prepended to
  // the action-clip list at submit time so slot 1 receives face footage.
  const faceUpload = useFileUpload({
    getPresignedUrl: async (file) => {
      return adminGetPresignedUpload(file.name, file.type || "video/mp4");
    },
  });

  // Job poller for the test job
  const poller = useJobPoller<TemplateJobStatusResponse>(testJobId, {
    fetchStatus: getTemplateJobStatus,
    isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
  });

  // Rerun: rerender with updated recipe (no Gemini needed)
  const handleRerun = useCallback(async () => {
    const sourceId = latestJob?.job_id || testJobId;
    if (!sourceId) return;
    setRerolling(true);
    setTestError(null);
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    const token = typeof window !== "undefined" ? sessionStorage.getItem("nova_admin_token") : null;
    try {
      // Try rerender first (no Gemini, uses locked slots)
      let res = await fetch(`${apiUrl}/admin/templates/${template.id}/rerender-job`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { "X-Admin-Token": token } : {}),
        },
        body: JSON.stringify({ source_job_id: sourceId }),
      });
      if (!res.ok) {
        // Fallback to reroll if rerender fails (slot mismatch etc.)
        res = await fetch(`${apiUrl}/template-jobs/${sourceId}/reroll`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
      }
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `Rerun failed (${res.status})`);
      }
      const data = await res.json();
      setTestJobId(data.job_id);
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "Rerun failed");
    } finally {
      setRerolling(false);
    }
  }, [latestJob, testJobId, template.id]);

  // Push completed test job to parent (for editor video sync)
  useEffect(() => {
    if (
      poller.data?.status === "template_ready" &&
      poller.data.assembly_plan?.output_url
    ) {
      const completedJob: LatestTestJob = {
        job_id: poller.data.job_id,
        output_url: poller.data.assembly_plan.output_url,
        base_output_url: poller.data.assembly_plan.base_output_url ?? null,
        clip_paths: upload.successfulPaths.length > 0
          ? upload.successfulPaths
          : latestJob?.clip_paths ?? [],
        has_rerender_data: true,
        created_at: poller.data.created_at,
      };
      setLatestJob(completedJob);
      onJobComplete?.(completedJob);
    }
  }, [poller.data, onJobComplete, upload.successfulPaths, latestJob]);

  // Fetch metrics
  useEffect(() => {
    adminGetMetrics(template.id).then(setMetrics).catch(() => {});
  }, [template.id]);

  const handleCreateTestJob = useCallback(async () => {
    if (hasIntroSlot && faceUpload.successfulPaths.length === 0) {
      setTestError("Önce Part 1'e yüz/intro klibi yükle");
      return;
    }
    if (upload.successfulPaths.length === 0) {
      setTestError(
        hasIntroSlot
          ? "Part 2'ye aksiyon klipleri yükle"
          : "Upload clips first",
      );
      return;
    }
    setCreating(true);
    setTestError(null);
    try {
      // Face clip first → matcher's highest-priority hook slot picks it up.
      const orderedPaths = hasIntroSlot
        ? [...faceUpload.successfulPaths, ...upload.successfulPaths]
        : upload.successfulPaths;
      const res = await adminCreateTestJob(template.id, {
        clip_gcs_paths: orderedPaths,
      });
      setTestJobId(res.job_id);
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "Failed to create test job");
    } finally {
      setCreating(false);
    }
  }, [template.id, upload.successfulPaths, faceUpload.successfulPaths, hasIntroSlot]);

  return (
    <div className="space-y-6">
      {/* Metrics summary */}
      {metrics && (
        <div className="grid grid-cols-4 gap-3">
          <MetricCard label="Total Jobs" value={metrics.total_jobs} />
          <MetricCard label="Successful" value={metrics.successful_jobs} />
          <MetricCard label="Failed" value={metrics.failed_jobs} />
          <MetricCard
            label="Last Test"
            value={metrics.last_job_at ? new Date(metrics.last_job_at).toLocaleDateString() : "Never"}
          />
        </div>
      )}

      {/* Slot-bound upload (mixed-media templates: video + photo, in slot order) */}
      {isSlotBound && (
        <AdminSlotBoundUpload
          template={template}
          slots={recipeSlots}
          onJobCreated={(jobId) => {
            setTestJobId(jobId);
            setTestError(null);
          }}
        />
      )}

      {/* Free-form upload (legacy: all-video templates) */}
      {!isSlotBound && (
      <div className="border border-zinc-800 rounded p-4 space-y-4">
        <h3 className="text-sm font-medium text-white">
          {hasIntroSlot ? "Test Clips (Part 1 + Part 2)" : "Upload Test Clips"}
        </h3>

        {/* Part 1 — Face/Intro dropzone (face templates only) */}
        {hasIntroSlot && (
          <div className="space-y-2">
            <p className="text-xs font-semibold text-amber-300 uppercase tracking-wide">
              Part 1 — Yüz / Intro klibi (1 video)
            </p>
            <div
              className={`border-2 border-dashed rounded-lg p-5 text-center transition-colors ${
                faceUpload.uploading
                  ? "border-amber-700/40 bg-amber-950/10"
                  : faceUpload.successfulPaths.length > 0
                  ? "border-amber-700/60 bg-amber-950/10"
                  : "border-amber-700/40 hover:border-amber-500/60"
              }`}
            >
              <input
                type="file"
                accept="video/mp4,video/quicktime"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  // Replace any existing face clip — only ever one allowed
                  faceUpload.files.forEach((existing) => faceUpload.removeFile(existing.id));
                  const entries = faceUpload.addFiles([f]);
                  faceUpload.startUpload(entries);
                }}
                disabled={faceUpload.uploading}
                className="hidden"
                id="face-clip-input"
              />
              <label
                htmlFor="face-clip-input"
                className="cursor-pointer text-sm text-zinc-400 hover:text-white"
              >
                {faceUpload.uploading
                  ? "Uploading..."
                  : faceUpload.successfulPaths.length > 0
                  ? "Yüz klibi yüklendi — değiştirmek için tıkla"
                  : "Yakın çekim yüz / röportaj klibi seç"}
              </label>
            </div>

            {faceUpload.files.length > 0 && (
              <div className="space-y-1">
                {faceUpload.files.map((f) => (
                  <div key={f.id} className="flex items-center gap-3 text-sm">
                    <span className="text-amber-300 truncate flex-1">{f.file.name}</span>
                    {f.error ? (
                      <span className="text-red-400 text-xs">{f.error}</span>
                    ) : f.progress === 100 ? (
                      <span className="text-green-400 text-xs">Done</span>
                    ) : (
                      <div className="w-24 bg-zinc-800 rounded-full h-1.5">
                        <div
                          className="bg-amber-500 h-full rounded-full transition-all"
                          style={{ width: `${f.progress}%` }}
                        />
                      </div>
                    )}
                    <button
                      onClick={() => faceUpload.removeFile(f.id)}
                      className="text-zinc-600 hover:text-zinc-400 text-xs"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Part 2 (face templates) / sole zone (others) */}
        {hasIntroSlot && (
          <p className="text-xs font-semibold text-zinc-300 uppercase tracking-wide mt-4">
            Part 2 — Aksiyon klipleri
          </p>
        )}
        <div
          className={`border-2 border-dashed rounded-lg p-6 text-center transition-colors ${
            upload.uploading ? "border-zinc-700 bg-zinc-900/50" : "border-zinc-700 hover:border-zinc-500"
          }`}
        >
          <input
            type="file"
            accept="video/mp4,video/quicktime"
            multiple
            onChange={(e) => {
              if (e.target.files) {
                const entries = upload.addFiles(Array.from(e.target.files));
                upload.startUpload(entries);
              }
            }}
            disabled={upload.uploading}
            className="hidden"
            id="test-clip-input"
          />
          <label
            htmlFor="test-clip-input"
            className="cursor-pointer text-sm text-zinc-400 hover:text-white"
          >
            {upload.uploading
              ? "Uploading..."
              : hasIntroSlot
              ? "Aksiyon kliplerini seç (multi-select)"
              : "Click to select clips or drag and drop"}
          </label>
        </div>

        {/* Upload progress */}
        {upload.files.length > 0 && (
          <div className="space-y-2">
            {upload.files.map((f) => (
              <div key={f.id} className="flex items-center gap-3 text-sm">
                <span className="text-zinc-300 truncate flex-1">{f.file.name}</span>
                {f.error ? (
                  <span className="text-red-400 text-xs">{f.error}</span>
                ) : f.progress === 100 ? (
                  <span className="text-green-400 text-xs">Done</span>
                ) : (
                  <div className="w-24 bg-zinc-800 rounded-full h-1.5">
                    <div
                      className="bg-blue-500 h-full rounded-full transition-all"
                      style={{ width: `${f.progress}%` }}
                    />
                  </div>
                )}
                <button
                  onClick={() => upload.removeFile(f.id)}
                  className="text-zinc-600 hover:text-zinc-400 text-xs"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Create test job button */}
        <div className="flex items-center gap-3">
          <button
            onClick={handleCreateTestJob}
            disabled={creating || upload.successfulPaths.length === 0 || template.analysis_status !== "ready"}
            className="px-4 py-2 text-sm bg-blue-700 hover:bg-blue-600 text-white rounded disabled:opacity-50"
          >
            {creating ? "Creating..." : "Create Test Job"}
          </button>
          {testError && <p className="text-red-400 text-sm">{testError}</p>}
          {upload.successfulPaths.length > 0 && (
            <span className="text-zinc-500 text-xs">{upload.successfulPaths.length} clip(s) ready</span>
          )}
        </div>
      </div>
      )}

      {/* Rerun with updated recipe */}
      {(latestJob || testJobId) && (
        <div className="border border-zinc-800 rounded p-4 space-y-3">
          <h3 className="text-sm font-medium text-white">Rerun with Updated Recipe</h3>
          <p className="text-xs text-zinc-500">
            Re-renders using clips from the latest job — no re-upload needed.
          </p>
          <button
            onClick={handleRerun}
            disabled={rerolling}
            className="px-4 py-2 text-sm bg-purple-700 hover:bg-purple-600 text-white rounded disabled:opacity-50"
          >
            {rerolling ? "Starting..." : "Rerun"}
          </button>
        </div>
      )}

      {/* Test job result */}
      {testJobId && (
        <div className="border border-zinc-800 rounded p-4 space-y-4">
          <h3 className="text-sm font-medium text-white">Test Job Result</h3>
          {poller.polling && (
            <div className="flex items-center gap-3">
              <div className="w-5 h-5 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
              <span className="text-zinc-400 text-sm">
                {poller.data?.status === "queued" ? "Waiting in queue..." :
                 poller.data?.status === "processing" ? "Processing clips..." :
                 "Working..."}
              </span>
            </div>
          )}
          {poller.error && <p className="text-red-400 text-sm">{poller.error}</p>}
          {poller.data?.status === "template_ready" && poller.data.assembly_plan?.output_url && (
            <EvalComparison
              outputUrl={poller.data.assembly_plan.output_url}
              templateUrl={playbackUrl}
              assemblyPlan={poller.data.assembly_plan}
            />
          )}
          {poller.data?.status === "processing_failed" && (
            <p className="text-red-400 text-sm">
              Test failed: {poller.data.error_detail ?? "Unknown error"}
            </p>
          )}
        </div>
      )}

      {/* Show latest job result when no active test job */}
      {!testJobId && latestJob?.output_url && (
        <div className="border border-zinc-800 rounded p-4 space-y-4">
          <h3 className="text-sm font-medium text-white">Latest Result</h3>
          <EvalComparison
            outputUrl={latestJob.output_url}
            templateUrl={playbackUrl}
            assemblyPlan={null}
          />
        </div>
      )}
    </div>
  );
}

// ── Eval Comparison ────────────────────────────────────────────────────────────

function EvalComparison({
  outputUrl,
  templateUrl,
  assemblyPlan,
}: {
  outputUrl: string;
  templateUrl: string | null;
  assemblyPlan: TemplateJobStatusResponse["assembly_plan"] | null;
}) {
  return (
    <div className="space-y-4">
      {/* Side-by-side video */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <p className="text-xs text-zinc-500 mb-2">Template Reference</p>
          {templateUrl ? <VideoPlayer url={templateUrl} /> : <VideoPlaceholder />}
        </div>
        <div>
          <p className="text-xs text-zinc-500 mb-2">Generated Output</p>
          <VideoPlayer url={outputUrl} />
        </div>
      </div>

      {/* Per-slot table */}
      {assemblyPlan?.steps && assemblyPlan.steps.length > 0 && (
        <div className="border border-zinc-800 rounded overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900">
              <tr>
                <th className="text-left px-3 py-2 text-zinc-500 font-medium">Slot</th>
                <th className="text-left px-3 py-2 text-zinc-500 font-medium">Duration</th>
                <th className="text-left px-3 py-2 text-zinc-500 font-medium">Clip</th>
                <th className="text-left px-3 py-2 text-zinc-500 font-medium">Energy</th>
              </tr>
            </thead>
            <tbody>
              {assemblyPlan.steps.map((step, i) => (
                <tr key={i} className="border-t border-zinc-800">
                  <td className="px-3 py-2 text-zinc-300">{step.slot?.position ?? i + 1}</td>
                  <td className="px-3 py-2 text-zinc-300">{step.slot?.target_duration_s?.toFixed(1)}s</td>
                  <td className="px-3 py-2 text-zinc-400 font-mono text-xs truncate max-w-[120px]">
                    {step.clip_id?.slice(0, 8)}
                  </td>
                  <td className="px-3 py-2">
                    <EnergyBar value={step.moment?.energy ?? 0} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Slot-bound upload (mixed-media: video + photo, in slot order) ─────────────

type AdminSlot = {
  position: number;
  target_duration_s: number;
  media_type: "video" | "photo";
};

type AdminSlotState = {
  slot: AdminSlot;
  file: File | null;
  uploading: boolean;
  error: string | null;
  gcsPath: string | null;
};

function AdminSlotBoundUpload({
  template,
  slots,
  onJobCreated,
}: {
  template: AdminTemplate;
  slots: AdminSlot[];
  onJobCreated: (jobId: string) => void;
}) {
  const [items, setItems] = useState<AdminSlotState[]>(
    () => slots.map((s) => ({ slot: s, file: null, uploading: false, error: null, gcsPath: null })),
  );
  const [phase, setPhase] = useState<"ready" | "uploading" | "creating" | "error">("ready");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const inputRefs = useRef<Array<HTMLInputElement | null>>([]);

  // Reset state if slots change (admin saved a new recipe)
  useEffect(() => {
    setItems(slots.map((s) => ({ slot: s, file: null, uploading: false, error: null, gcsPath: null })));
    setPhase("ready");
    setSubmitError(null);
  }, [slots]);

  function patch(idx: number, p: Partial<AdminSlotState>) {
    setItems((prev) => prev.map((s, i) => (i === idx ? { ...s, ...p } : s)));
  }

  function pickFile(idx: number, file: File) {
    const slot = items[idx].slot;
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (slot.media_type === "photo") {
      const ok =
        ["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"].includes(file.type) ||
        ext === "heic" || ext === "heif";
      if (!ok) return patch(idx, { file: null, error: "Pick a photo (JPEG/PNG/WEBP/HEIC)" });
      if (file.size > 25 * 1024 * 1024) return patch(idx, { file: null, error: "Image > 25MB" });
    } else {
      const ok = ["video/mp4", "video/quicktime"].includes(file.type);
      if (!ok) return patch(idx, { file: null, error: "Pick a video (MP4/MOV)" });
      if (file.size > 4 * 1024 * 1024 * 1024) return patch(idx, { file: null, error: "Video > 4GB" });
    }
    patch(idx, { file, error: null, gcsPath: null });
  }

  async function uploadOne(idx: number, current: AdminSlotState): Promise<string> {
    if (!current.file) throw new Error("No file");
    patch(idx, { uploading: true, error: null });

    if (current.slot.media_type === "photo") {
      const { gcs_path } = await uploadTemplatePhoto({
        templateId: template.id,
        slotPosition: current.slot.position,
        file: current.file,
      });
      patch(idx, { uploading: false, gcsPath: gcs_path });
      return gcs_path;
    }

    // Video: admin presigned upload. GCS signs against the exact content-type,
    // and uploadFileToGcs normalises video/quicktime -> video/mp4 before PUT,
    // so we must request the presigned URL with the *same* normalised type.
    // Otherwise GCS rejects the PUT with 403 (signature mismatch).
    const signingContentType = normaliseMimeType(current.file.type);
    const presigned = await adminGetPresignedUpload(
      current.file.name,
      signingContentType,
    );
    await uploadFileToGcs(presigned.upload_url, current.file);
    patch(idx, { uploading: false, gcsPath: presigned.gcs_path });
    return presigned.gcs_path;
  }

  async function handleSubmit() {
    if (items.some((s) => !s.file)) {
      setSubmitError("Fill every slot before submitting.");
      return;
    }
    setSubmitError(null);
    setPhase("uploading");
    try {
      const paths: string[] = [];
      for (let i = 0; i < items.length; i++) {
        const p = await uploadOne(i, items[i]);
        paths.push(p);
      }
      setPhase("creating");
      const res = await adminCreateTestJob(template.id, { clip_gcs_paths: paths });
      onJobCreated(res.job_id);
      setPhase("ready");
    } catch (err) {
      setPhase("error");
      setSubmitError(err instanceof Error ? err.message : "Failed");
    }
  }

  const allReady = items.every((s) => s.file);
  const submitting = phase === "uploading" || phase === "creating";

  return (
    <div className="border border-zinc-800 rounded p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-white">Upload Test Clips ({items.length} slots)</h3>
        <span className="text-xs text-zinc-500">slot order matters</span>
      </div>

      <ol className="space-y-2">
        {items.map((s, i) => {
          const isPhoto = s.slot.media_type === "photo";
          const accept = isPhoto
            ? "image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif"
            : "video/mp4,video/quicktime";
          return (
            <li key={s.slot.position} className="border border-zinc-800 rounded p-3 bg-zinc-900/40">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-zinc-300">
                  Slot {s.slot.position} · {isPhoto ? "Photo" : "Video"}
                </span>
                <span className="text-xs text-zinc-500">~{s.slot.target_duration_s.toFixed(1)}s</span>
              </div>
              <button
                onClick={() => inputRefs.current[i]?.click()}
                disabled={submitting}
                className="w-full px-3 py-2 rounded text-xs border border-zinc-700 hover:border-zinc-500 disabled:opacity-50 text-left text-zinc-300"
              >
                {s.file ? s.file.name : isPhoto ? "Choose a photo…" : "Choose a video…"}
              </button>
              <input
                ref={(el) => { inputRefs.current[i] = el; }}
                type="file"
                accept={accept}
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) pickFile(i, f);
                  e.target.value = "";
                }}
              />
              {s.uploading && <p className="mt-1 text-xs text-blue-400">Uploading…</p>}
              {s.gcsPath && !s.uploading && <p className="mt-1 text-xs text-green-400">✓ Uploaded</p>}
              {s.error && <p className="mt-1 text-xs text-red-400">{s.error}</p>}
            </li>
          );
        })}
      </ol>

      {submitError && (
        <div className="bg-red-900/40 border border-red-700 rounded px-3 py-2 text-xs text-red-300">
          {submitError}
        </div>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={handleSubmit}
          disabled={!allReady || submitting || template.analysis_status !== "ready"}
          className="px-4 py-2 text-sm bg-blue-700 hover:bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {phase === "uploading" ? "Uploading…" : phase === "creating" ? "Starting…" : "Create Test Job"}
        </button>
        <span className="text-xs text-zinc-500">
          {items.filter((s) => s.gcsPath).length}/{items.length} ready
        </span>
      </div>
    </div>
  );
}

// ── Settings Tab ───────────────────────────────────────────────────────────────

function SettingsTab({
  template,
  onSave,
}: {
  template: AdminTemplate;
  onSave: (t: AdminTemplate) => void;
}) {
  const [name, setName] = useState(template.name);
  const [description, setDescription] = useState(template.description ?? "");
  const [sourceUrl, setSourceUrl] = useState(template.source_url ?? "");
  const [clipsMin, setClipsMin] = useState(template.required_clips_min);
  const [clipsMax, setClipsMax] = useState(template.required_clips_max);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [togglingMusic, setTogglingMusic] = useState(false);
  const [togglingIntroSlot, setTogglingIntroSlot] = useState(false);

  const isMusicParent = (template.template_type ?? "standard") === "music_parent";
  const isMusicChild = (template.template_type ?? "standard") === "music_child";
  const isAudioOnly = (template.template_type ?? "standard") === "audio_only";
  const hasIntroSlot = template.has_intro_slot ?? false;

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      const updated = await adminUpdateTemplate(template.id, {
        name,
        description: description || undefined,
        source_url: sourceUrl || undefined,
        required_clips_min: clipsMin,
        required_clips_max: clipsMax,
      });
      onSave(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const handleToggleMusic = async () => {
    const newType = isMusicParent ? "standard" : "music_parent";
    if (isMusicParent && !confirm("Disable Music Variants? You must delete all sub-templates first.")) return;
    setTogglingMusic(true);
    try {
      const updated = await adminUpdateTemplate(template.id, {
        template_type: newType,
      });
      onSave(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Toggle failed");
    } finally {
      setTogglingMusic(false);
    }
  };

  const handleToggleIntroSlot = async () => {
    setTogglingIntroSlot(true);
    try {
      const updated = await adminUpdateTemplate(template.id, {
        has_intro_slot: !hasIntroSlot,
      });
      onSave(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Toggle failed");
    } finally {
      setTogglingIntroSlot(false);
    }
  };

  return (
    <div className="max-w-xl space-y-5">
      <Field label="Name">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
        />
      </Field>

      <Field label="Description">
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500 resize-none"
        />
      </Field>

      <Field label="Source URL">
        <input
          value={sourceUrl}
          onChange={(e) => setSourceUrl(e.target.value)}
          placeholder="https://www.tiktok.com/..."
          className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
        />
      </Field>

      <div className="grid grid-cols-2 gap-4">
        <Field label="Min Clips">
          <input
            type="number"
            value={clipsMin}
            onChange={(e) => setClipsMin(Number(e.target.value))}
            min={1}
            max={30}
            className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
          />
        </Field>
        <Field label="Max Clips">
          <input
            type="number"
            value={clipsMax}
            onChange={(e) => setClipsMax(Number(e.target.value))}
            min={1}
            max={30}
            className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
          />
        </Field>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-4 py-2 text-sm bg-white text-black rounded hover:bg-zinc-200 disabled:opacity-50"
        >
          {saving ? "Saving..." : "Save Settings"}
        </button>
        {saved && <span className="text-green-400 text-sm">Saved</span>}
      </div>

      {/* Intro-slot toggle — gates the two-part upload (one pinned clip/photo
          for slot 1 + action clips for the rest) in TestTab and the public
          template page. Stored in recipe_cached.has_intro_slot; survives
          re-analysis via _ROUTING_ONLY_RECIPE_KEYS. */}
      {!isMusicChild && !isAudioOnly && (
        <div className="border-t border-zinc-800 pt-5 mt-6">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-white font-medium">Has intro slot</p>
              <p className="text-xs text-zinc-500 mt-0.5">
                Reserve slot 1 for a user-uploaded intro clip or photo; remaining slots take action clips
              </p>
            </div>
            <button
              onClick={handleToggleIntroSlot}
              disabled={togglingIntroSlot}
              role="switch"
              aria-checked={hasIntroSlot}
              className={`relative w-11 h-6 rounded-full transition-colors ${
                hasIntroSlot ? "bg-blue-600" : "bg-zinc-700"
              } ${togglingIntroSlot ? "opacity-50" : ""}`}
            >
              <span
                className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${
                  hasIntroSlot ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          </div>
        </div>
      )}

      {/* Music Variants toggle — only for standard/music_parent (not children or audio_only) */}
      {!isMusicChild && !isAudioOnly && (
        <div className="border-t border-zinc-800 pt-5 mt-6">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-white font-medium">Enable Music Variants</p>
              <p className="text-xs text-zinc-500 mt-0.5">
                Create beat-synced sub-templates for different songs
              </p>
            </div>
            <button
              onClick={handleToggleMusic}
              disabled={togglingMusic}
              role="switch"
              aria-checked={isMusicParent}
              className={`relative w-11 h-6 rounded-full transition-colors ${
                isMusicParent ? "bg-blue-600" : "bg-zinc-700"
              } ${togglingMusic ? "opacity-50" : ""}`}
            >
              <span
                className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${
                  isMusicParent ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          </div>
        </div>
      )}

      <div className="border-t border-zinc-800 pt-4 mt-6">
        {template.gcs_path ? (
          <>
            <p className="text-xs text-zinc-500 mb-1">GCS Path</p>
            <p className="text-sm text-zinc-300 font-mono">{template.gcs_path}</p>
          </>
        ) : (
          <p className="text-xs text-zinc-500 mb-1">Audio-only (no video file)</p>
        )}
        <p className="text-xs text-zinc-500 mt-3 mb-1">Template ID</p>
        <p className="text-sm text-zinc-300 font-mono">{template.id}</p>
      </div>
    </div>
  );
}

// ── Shared Components ──────────────────────────────────────────────────────────

function VideoPlayer({ url }: { url: string }) {
  return (
    <div className="w-[280px] aspect-[9/16] bg-black rounded overflow-hidden flex-shrink-0">
      <video
        src={url}
        controls
        className="w-full h-full object-contain"
        playsInline
      />
    </div>
  );
}

function VideoPlaceholder() {
  return (
    <div className="w-[280px] aspect-[9/16] bg-zinc-900 rounded flex items-center justify-center text-zinc-600 text-sm">
      No video
    </div>
  );
}

function AudioOnlyPlaceholder() {
  return (
    <div className="w-[280px] aspect-[9/16] bg-zinc-900 rounded flex flex-col items-center justify-center gap-2 flex-shrink-0">
      <span className="text-3xl">🎵</span>
      <span className="text-zinc-500 text-sm">Audio-only template</span>
      <span className="text-zinc-600 text-xs">No reference video</span>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    analyzing: "bg-amber-900/40 text-amber-400",
    ready: "bg-green-900/40 text-green-400",
    failed: "bg-red-900/40 text-red-400",
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${colors[status] ?? "bg-zinc-700 text-zinc-300"}`}>
      {status}
    </span>
  );
}

function MetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded p-3">
      <p className="text-xs text-zinc-500">{label}</p>
      <p className="text-lg font-semibold text-white mt-0.5">{value}</p>
    </div>
  );
}

function EnergyBar({ value }: { value: number }) {
  const pct = Math.min(Math.max(value * 100, 0), 100);
  return (
    <div className="w-16 bg-zinc-800 rounded-full h-1.5">
      <div
        className="bg-blue-500 h-full rounded-full"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm text-zinc-400 mb-1.5">{label}</label>
      {children}
    </div>
  );
}

function PageSkeleton() {
  return (
    <div className="p-8 space-y-4 animate-pulse">
      <div className="h-6 w-48 bg-zinc-800 rounded" />
      <div className="h-4 w-32 bg-zinc-800 rounded" />
      <div className="h-10 w-full bg-zinc-800 rounded mt-6" />
      <div className="h-64 w-full bg-zinc-800 rounded" />
    </div>
  );
}
