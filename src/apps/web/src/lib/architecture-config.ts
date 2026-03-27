/**
 * Architecture Config — single source of truth for the Nova system topology.
 *
 * MODULE GRAPH (L1 → L2):
 *
 *   [Upload] ──job_id──▶ [Processing] ──candidates──▶ [Clips] ──rendered──▶ [Delivery]
 *       │                      │                          │
 *       │               [Templates] ◀────────────────────┘
 *       │
 *   [PostgreSQL]  [Redis]  [GCS]
 *
 * Update this file when the architecture changes.
 * Tests validate structure: no circular deps, all L2s have parents, all files exist.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface BusinessContext {
  /** What this does for the user, in plain language */
  userFacing: string;
  /** Why it matters for the business */
  businessImpact: string;
  /** Key metric this module affects */
  metric: string;
  /** Current status or phase */
  status: "live" | "building" | "planned";
}

export interface Module {
  id: string;
  name: string;
  description: string;
  level: "L1" | "L2";
  files: string[];
  githubLabel: string;
  dependsOn: string[];
  produces: string[];
  children?: Record<string, Module>;
  /** Data store nodes use a different visual style */
  isDataStore?: boolean;
  /** Business-facing context (shown in Business view) */
  business?: BusinessContext;
}

// ---------------------------------------------------------------------------
// L2 Sub-modules
// ---------------------------------------------------------------------------

const uploadChildren: Record<string, Module> = {
  upload_ui: {
    id: "upload_ui",
    name: "Nova Studio UI",
    description: "Drag-drop upload interface with platform selection",
    level: "L2",
    files: ["src/apps/web/src/app/nova-studio/page.tsx"],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["file object", "selected platforms"],
  },
  presigned: {
    id: "presigned",
    name: "Presigned URLs",
    description: "GCS signed URL generation for client-side upload",
    level: "L2",
    files: [
      "src/apps/api/app/routes/uploads.py",
      "src/apps/api/app/storage.py",
    ],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["upload_url", "job_id", "gcs_path"],
  },
  validation: {
    id: "validation",
    name: "File Validation",
    description: "Client + server file type, size, and duration checks",
    level: "L2",
    files: ["src/apps/api/app/routes/uploads.py"],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["validated file metadata"],
  },
};

const processingChildren: Record<string, Module> = {
  probe: {
    id: "probe",
    name: "Probe",
    description: "FFprobe metadata extraction (duration, resolution, fps, codec)",
    level: "L2",
    files: ["src/apps/api/app/pipeline/probe.py"],
    githubLabel: "module:processing",
    dependsOn: ["presigned"],
    produces: ["probe_metadata JSON"],
  },
  transcribe: {
    id: "transcribe",
    name: "Transcribe",
    description: "Whisper/Gemini speech-to-text with word-level timestamps",
    level: "L2",
    files: ["src/apps/api/app/pipeline/transcribe.py"],
    githubLabel: "module:processing",
    dependsOn: ["probe"],
    produces: ["transcript JSON with timestamps"],
  },
  scene_detect: {
    id: "scene_detect",
    name: "Scene Detection",
    description: "PySceneDetect cut point detection for segment boundaries",
    level: "L2",
    files: ["src/apps/api/app/pipeline/scene_detect.py"],
    githubLabel: "module:processing",
    dependsOn: ["probe"],
    produces: ["scene cut timestamps"],
  },
  score: {
    id: "score",
    name: "Scoring",
    description: "Hook strength + engagement scoring, top-9 candidate selection",
    level: "L2",
    files: ["src/apps/api/app/pipeline/score.py"],
    githubLabel: "module:processing",
    dependsOn: ["transcribe", "scene_detect"],
    produces: ["top 9 ranked candidates"],
  },
  gemini: {
    id: "gemini",
    name: "Gemini Analysis",
    description: "Gemini AI analysis for hook scoring, creative direction, recipes",
    level: "L2",
    files: [
      "src/apps/api/app/pipeline/agents/gemini_analyzer.py",
      "src/apps/api/app/pipeline/agents/hook_scorer.py",
    ],
    githubLabel: "module:processing",
    dependsOn: ["probe"],
    produces: ["analysis JSON", "hook scores"],
  },
};

const clipsChildren: Record<string, Module> = {
  reframe: {
    id: "reframe",
    name: "Reframe",
    description: "9:16 aspect ratio scaling + H.264/AAC export via FFmpeg",
    level: "L2",
    files: ["src/apps/api/app/pipeline/reframe.py"],
    githubLabel: "module:clips",
    dependsOn: ["score"],
    produces: ["1080x1920 H.264 video file"],
  },
  captions: {
    id: "captions",
    name: "Captions",
    description: "ASS subtitle generation from Whisper timestamps",
    level: "L2",
    files: [
      "src/apps/api/app/pipeline/captions.py",
      "src/apps/api/app/pipeline/ass_utils.py",
    ],
    githubLabel: "module:clips",
    dependsOn: ["transcribe"],
    produces: ["ASS subtitle file"],
  },
  text_overlay: {
    id: "text_overlay",
    name: "Text Overlay",
    description: "Render captions and animated text onto video frames",
    level: "L2",
    files: ["src/apps/api/app/pipeline/text_overlay.py"],
    githubLabel: "module:clips",
    dependsOn: ["captions", "reframe"],
    produces: ["video with burned-in captions"],
  },
  thumbnail: {
    id: "thumbnail",
    name: "Thumbnail",
    description: "Extract best keyframe for video thumbnail",
    level: "L2",
    files: ["src/apps/api/app/pipeline/thumbnail.py"],
    githubLabel: "module:clips",
    dependsOn: ["reframe"],
    produces: ["thumbnail image"],
  },
  copy_writer: {
    id: "copy_writer",
    name: "Copy Writer",
    description: "Platform-specific captions and hashtags via Gemini",
    level: "L2",
    files: ["src/apps/api/app/pipeline/agents/copy_writer.py"],
    githubLabel: "module:clips",
    dependsOn: ["transcribe", "score"],
    produces: ["platform_copy JSON (TikTok, IG, YouTube)"],
  },
};

const templateChildren: Record<string, Module> = {
  template_matcher: {
    id: "template_matcher",
    name: "Template Matcher",
    description: "Match uploaded clips to template slots by energy and duration",
    level: "L2",
    files: ["src/apps/api/app/pipeline/template_matcher.py"],
    githubLabel: "module:templates",
    dependsOn: ["gemini"],
    produces: ["assembly_plan JSON"],
  },
  transitions: {
    id: "transitions",
    name: "Transitions",
    description: "FFmpeg transition filter chains between clips",
    level: "L2",
    files: ["src/apps/api/app/pipeline/transitions.py"],
    githubLabel: "module:templates",
    dependsOn: ["template_matcher"],
    produces: ["FFmpeg filter graph"],
  },
  compositor: {
    id: "compositor",
    name: "Compositor",
    description: "Assemble matched clips into final template video with effects",
    level: "L2",
    files: ["src/apps/api/app/tasks/template_orchestrate.py"],
    githubLabel: "module:templates",
    dependsOn: ["template_matcher", "transitions"],
    produces: ["final assembled video"],
  },
};

const deliveryChildren: Record<string, Module> = {
  results_page: {
    id: "results_page",
    name: "Results Page",
    description: "Clip cards with scores, thumbnails, download links, and copy",
    level: "L2",
    files: [
      "src/apps/web/src/app/jobs/[id]/page.tsx",
      "src/apps/web/src/app/template-jobs/[id]/page.tsx",
    ],
    githubLabel: "module:delivery",
    dependsOn: ["reframe", "copy_writer", "compositor"],
    produces: ["rendered clip cards in browser"],
  },
  posting: {
    id: "posting",
    name: "Platform Posting",
    description: "Instagram, YouTube, TikTok upload integrations (Phase 2)",
    level: "L2",
    files: [],
    githubLabel: "module:delivery",
    dependsOn: ["results_page"],
    produces: ["posted content on platforms"],
  },
};

// ---------------------------------------------------------------------------
// L1 Pipeline Modules
// ---------------------------------------------------------------------------

export const modules: Record<string, Module> = {
  upload: {
    id: "upload",
    name: "Upload & Ingest",
    description: "Client-side upload to GCS via signed URLs",
    level: "L1",
    files: [
      "src/apps/web/src/app/nova-studio/page.tsx",
      "src/apps/api/app/routes/uploads.py",
      "src/apps/api/app/storage.py",
    ],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["job_id", "raw_storage_path"],
    children: uploadChildren,
    business: {
      userFacing: "Where creators drop their raw footage. One drag, one button. No settings.",
      businessImpact: "First touch point. If upload feels slow or confusing, users leave before seeing any value.",
      metric: "Upload completion rate (target: >90%)",
      status: "live",
    },
  },
  processing: {
    id: "processing",
    name: "Processing",
    description: "Video analysis pipeline: probe, transcribe, scene detect, score",
    level: "L1",
    files: [
      "src/apps/api/app/tasks/orchestrate.py",
      "src/apps/api/app/pipeline/probe.py",
      "src/apps/api/app/pipeline/transcribe.py",
      "src/apps/api/app/pipeline/scene_detect.py",
      "src/apps/api/app/pipeline/score.py",
    ],
    githubLabel: "module:processing",
    dependsOn: ["upload"],
    produces: ["top 9 clip candidates (ranked)"],
    children: processingChildren,
    business: {
      userFacing: "AI watches the video and finds the best moments. The user waits 2-5 min.",
      businessImpact: "This is the core AI magic. Quality of clip selection determines if users trust Nova over manual editing.",
      metric: "Processing time <8 min (SLA), clip quality score vs human baseline",
      status: "live",
    },
  },
  clips: {
    id: "clips",
    name: "Clip Generation",
    description: "Reframe, caption, thumbnail, and copy generation for top clips",
    level: "L1",
    files: [
      "src/apps/api/app/pipeline/reframe.py",
      "src/apps/api/app/pipeline/captions.py",
      "src/apps/api/app/pipeline/text_overlay.py",
      "src/apps/api/app/pipeline/thumbnail.py",
    ],
    githubLabel: "module:clips",
    dependsOn: ["processing"],
    produces: ["3 rendered clips + platform copy"],
    children: clipsChildren,
    business: {
      userFacing: "Turns raw moments into ready-to-post shorts with captions and platform-specific copy.",
      businessImpact: "This is where 'raw footage' becomes 'content'. The output quality here is what users judge Nova by.",
      metric: "% of users who post without editing copy (target: >70%)",
      status: "live",
    },
  },
  templates: {
    id: "templates",
    name: "Template Mode",
    description: "Match clips to template slots, assemble with transitions and effects",
    level: "L1",
    files: [
      "src/apps/api/app/tasks/template_orchestrate.py",
      "src/apps/api/app/pipeline/template_matcher.py",
      "src/apps/api/app/pipeline/transitions.py",
    ],
    githubLabel: "module:templates",
    dependsOn: ["processing"],
    produces: ["assembled template video"],
    children: templateChildren,
    business: {
      userFacing: "Pick a trending TikTok template, upload your clips, get a polished video matching that style.",
      businessImpact: "Differentiator from OpusClip. Templates let creators ride trends without editing skills.",
      metric: "Template completion rate, re-roll rate (<30% means templates are good enough first try)",
      status: "live",
    },
  },
  delivery: {
    id: "delivery",
    name: "Results & Delivery",
    description: "Clip results UI, download, and platform posting",
    level: "L1",
    files: [
      "src/apps/web/src/app/jobs/[id]/page.tsx",
      "src/apps/web/src/app/template-jobs/[id]/page.tsx",
    ],
    githubLabel: "module:delivery",
    dependsOn: ["clips", "templates"],
    produces: ["posted content / downloaded clips"],
    children: deliveryChildren,
    business: {
      userFacing: "See your 3 best clips ranked, preview them, and post to platforms with one click.",
      businessImpact: "The 'zero-decision' promise lives here. If posting requires decisions, we've failed.",
      metric: "Click-to-post rate, time from 'clips ready' to 'posted' (<30s target)",
      status: "building",
    },
  },

  // Data stores — separate visual style
  postgresql: {
    id: "postgresql",
    name: "PostgreSQL",
    description: "Job metadata, user state, OAuth tokens",
    level: "L1",
    files: ["src/apps/api/app/models.py", "src/apps/api/app/database.py"],
    githubLabel: "module:infra",
    dependsOn: [],
    produces: [],
    isDataStore: true,
    business: {
      userFacing: "Remembers your jobs, your connected accounts, and your preferences.",
      businessImpact: "Source of truth for everything. If this is down, nothing works.",
      metric: "Uptime, query latency p99",
      status: "live",
    },
  },
  redis: {
    id: "redis",
    name: "Redis",
    description: "Celery job queue for async processing",
    level: "L1",
    files: ["src/apps/api/app/celery_app.py"],
    githubLabel: "module:infra",
    dependsOn: [],
    produces: [],
    isDataStore: true,
    business: {
      userFacing: "The waiting line. When you upload a video, it joins the queue here.",
      businessImpact: "Queue depth = user wait time. If this backs up, processing SLA breaks.",
      metric: "Queue depth, job wait time before processing starts",
      status: "live",
    },
  },
  gcs: {
    id: "gcs",
    name: "GCS / S3",
    description: "Raw uploads + processed video outputs",
    level: "L1",
    files: ["src/apps/api/app/storage.py"],
    githubLabel: "module:infra",
    dependsOn: [],
    produces: [],
    isDataStore: true,
    business: {
      userFacing: "Where all videos live. Your raw uploads and the finished clips.",
      businessImpact: "Biggest cost driver at scale. Every video is stored twice (raw + processed).",
      metric: "Storage cost per user, download speed for clip previews",
      status: "live",
    },
  },
};

// ---------------------------------------------------------------------------
// L1 Edges (dependency arrows between L1 modules)
// ---------------------------------------------------------------------------

export interface Edge {
  source: string;
  target: string;
  label: string;
  /** Business-friendly label for non-technical view */
  businessLabel: string;
}

export const edges: Edge[] = [
  {
    source: "upload",
    target: "processing",
    label: "job_id + raw_storage_path",
    businessLabel: "User's video enters the pipeline",
  },
  {
    source: "processing",
    target: "clips",
    label: "top 9 candidates (ranked)",
    businessLabel: "Best 9 moments found, top 3 shown",
  },
  {
    source: "processing",
    target: "templates",
    label: "analysis + beat data",
    businessLabel: "Video analyzed for template matching",
  },
  {
    source: "clips",
    target: "delivery",
    label: "3 rendered clips + copy",
    businessLabel: "3 ready-to-post shorts with captions",
  },
  {
    source: "templates",
    target: "delivery",
    label: "assembled template video",
    businessLabel: "Finished template video ready to post",
  },
  // Data store connections
  { source: "upload", target: "gcs", label: "raw video file", businessLabel: "Video stored in cloud" },
  { source: "clips", target: "gcs", label: "processed clips", businessLabel: "Finished clips saved" },
  { source: "processing", target: "redis", label: "Celery task dispatch", businessLabel: "Job queued for processing" },
  { source: "processing", target: "postgresql", label: "job status updates", businessLabel: "Progress tracked" },
  { source: "delivery", target: "postgresql", label: "clip metadata reads", businessLabel: "Clip info loaded" },
];

// ---------------------------------------------------------------------------
// Job Status → Module Mapping (for live activity overlay)
// ---------------------------------------------------------------------------

export const JOB_STATUS_MODULE_MAP: Record<
  string,
  { moduleId: string; visual: "pulse" | "check" | "check-yellow" | "error" }
> = {
  queued: { moduleId: "upload", visual: "pulse" },
  processing: { moduleId: "processing", visual: "pulse" },
  clips_ready: { moduleId: "clips", visual: "check" },
  clips_ready_partial: { moduleId: "clips", visual: "check-yellow" },
  posting: { moduleId: "delivery", visual: "pulse" },
  done: { moduleId: "delivery", visual: "check" },
  processing_failed: { moduleId: "processing", visual: "error" },
  posting_failed: { moduleId: "delivery", visual: "error" },
  // Template job statuses
  template_ready: { moduleId: "templates", visual: "check" },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Get all L1 modules (non-data-store pipeline modules) */
export function getPipelineModules(): Module[] {
  return Object.values(modules).filter((m) => !m.isDataStore);
}

/** Get all data store modules */
export function getDataStoreModules(): Module[] {
  return Object.values(modules).filter((m) => m.isDataStore);
}

/** Get direct downstream dependents of a module (modules that depend on it) */
export function getDirectDependents(moduleId: string): Module[] {
  const allModules = Object.values(modules);
  const allL2Modules = allModules.flatMap((m) =>
    m.children ? Object.values(m.children) : []
  );
  const all = [...allModules, ...allL2Modules];
  return all.filter((m) => m.dependsOn.includes(moduleId));
}

/** Flatten all modules (L1 + L2) into a single list */
export function getAllModules(): Module[] {
  const l1 = Object.values(modules);
  const l2 = l1.flatMap((m) => (m.children ? Object.values(m.children) : []));
  return [...l1, ...l2];
}

/** Count children for an L1 module */
export function getChildCount(moduleId: string): number {
  const mod = modules[moduleId];
  return mod?.children ? Object.keys(mod.children).length : 0;
}
