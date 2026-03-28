"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type AdminTemplate,
  type RecipeVersionItem,
  type TemplateMetrics,
  adminCreateTestJob,
  adminGetMetrics,
  adminGetPresignedUpload,
  adminGetRecipeHistory,
  adminGetTemplate,
  adminReanalyzeTemplate,
  adminUpdateTemplate,
} from "@/lib/admin-api";
import {
  type TemplateJobStatusResponse,
  getTemplateJobStatus,
  getTemplatePlaybackUrl,
} from "@/lib/api";
import { useFileUpload } from "@/hooks/useFileUpload";
import { useJobPoller } from "@/hooks/useJobPoller";

// ── Types ──────────────────────────────────────────────────────────────────────

type TabId = "recipe" | "test" | "settings";

const TABS: { id: TabId; label: string }[] = [
  { id: "recipe", label: "Recipe" },
  { id: "test", label: "Test" },
  { id: "settings", label: "Settings" },
];

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

  // Fetch template data
  useEffect(() => {
    adminGetTemplate(id)
      .then((t) => {
        setTemplate(t);
        // Also get playback URL for video player
        getTemplatePlaybackUrl(id)
          .then((r) => setPlaybackUrl(r.url))
          .catch(() => {}); // playback may not be ready yet
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [id]);

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
    if (!confirm("Re-run Gemini analysis? This will take a few minutes.")) return;
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

  return (
    <div className="flex flex-col min-h-screen">
      {/* Header */}
      <div className="border-b border-zinc-800 px-6 py-4">
        <button onClick={() => router.push("/admin")} className="text-sm text-zinc-500 hover:text-zinc-300 mb-2 block">
          ← Back to dashboard
        </button>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-white">{template.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={template.analysis_status} />
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
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setTab(tab.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
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

      {/* Tab content */}
      <div className="flex-1 overflow-auto px-6 py-6">
        {activeTab === "recipe" && (
          <RecipeTab
            template={template}
            playbackUrl={playbackUrl}
            onRefresh={refreshTemplate}
          />
        )}
        {activeTab === "test" && (
          <TestTab template={template} playbackUrl={playbackUrl} />
        )}
        {activeTab === "settings" && (
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

  if (template.analysis_status === "analyzing") {
    return (
      <div className="flex gap-6">
        {playbackUrl && <VideoPlayer url={playbackUrl} />}
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
}: {
  template: AdminTemplate;
  playbackUrl: string | null;
}) {
  const [testJobId, setTestJobId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<TemplateMetrics | null>(null);

  // File upload for test clips
  const upload = useFileUpload({
    getPresignedUrl: async (file) => {
      return adminGetPresignedUpload(file.name, file.type || "video/mp4");
    },
  });

  // Job poller for the test job
  const poller = useJobPoller<TemplateJobStatusResponse>(testJobId, {
    fetchStatus: getTemplateJobStatus,
    isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
  });

  // Fetch metrics
  useEffect(() => {
    adminGetMetrics(template.id).then(setMetrics).catch(() => {});
  }, [template.id]);

  const handleCreateTestJob = useCallback(async () => {
    if (upload.successfulPaths.length === 0) {
      setTestError("Upload clips first");
      return;
    }
    setCreating(true);
    setTestError(null);
    try {
      const res = await adminCreateTestJob(template.id, {
        clip_gcs_paths: upload.successfulPaths,
      });
      setTestJobId(res.job_id);
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "Failed to create test job");
    } finally {
      setCreating(false);
    }
  }, [template.id, upload.successfulPaths]);

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

      {/* Upload clips section */}
      <div className="border border-zinc-800 rounded p-4 space-y-4">
        <h3 className="text-sm font-medium text-white">Upload Test Clips</h3>

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
            {upload.uploading ? "Uploading..." : "Click to select clips or drag and drop"}
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
  assemblyPlan: TemplateJobStatusResponse["assembly_plan"];
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

      <div className="border-t border-zinc-800 pt-4 mt-6">
        <p className="text-xs text-zinc-500 mb-1">GCS Path</p>
        <p className="text-sm text-zinc-300 font-mono">{template.gcs_path}</p>
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
