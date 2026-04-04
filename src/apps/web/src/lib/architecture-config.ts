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
    name: "Upload UI",
    description: "Drag-drop upload interface with platform selection",
    level: "L2",
    files: ["src/apps/web/src/app/page.tsx"],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["file object", "selected platforms"],
    business: {
      userFacing: "The upload page. Creator drags a video, picks platforms, hits one button.",
      businessImpact: "First impression. If this feels clunky, creators bounce before seeing value.",
      metric: "Upload start-to-complete time",
      status: "live",
    },
  },
  presigned: {
    id: "presigned",
    name: "Secure Upload Link",
    description: "GCS signed URL generation for client-side upload",
    level: "L2",
    files: [
      "src/apps/api/app/routes/uploads.py",
      "src/apps/api/app/storage.py",
    ],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["upload_url", "job_id", "gcs_path"],
    business: {
      userFacing: "Generates a secure, temporary link so the video goes straight to cloud storage.",
      businessImpact: "Keeps uploads fast. Video goes directly to storage, not through our server.",
      metric: "Upload speed (MB/s)",
      status: "live",
    },
  },
  validation: {
    id: "validation",
    name: "File Check",
    description: "Client + server file type, size, and duration checks",
    level: "L2",
    files: ["src/apps/api/app/routes/uploads.py"],
    githubLabel: "module:upload",
    dependsOn: [],
    produces: ["validated file metadata"],
    business: {
      userFacing: "Rejects bad files before upload starts. Wrong format? Too big? Told immediately.",
      businessImpact: "Prevents wasted processing time on files we can't handle.",
      metric: "Rejection rate, clarity of error messages",
      status: "live",
    },
  },
};

const processingChildren: Record<string, Module> = {
  probe: {
    id: "probe",
    name: "Video Scan",
    description: "FFprobe metadata extraction (duration, resolution, fps, codec)",
    level: "L2",
    files: ["src/apps/api/app/pipeline/probe.py"],
    githubLabel: "module:processing",
    dependsOn: ["presigned"],
    produces: ["probe_metadata JSON"],
    business: {
      userFacing: "Reads the video file to understand what we're working with: length, quality, format.",
      businessImpact: "Catches problems early. A corrupt file is rejected here, not 5 minutes later.",
      metric: "Probe time (<2s), rejection accuracy",
      status: "live",
    },
  },
  transcribe: {
    id: "transcribe",
    name: "Speech-to-Text",
    description: "Whisper/Gemini speech-to-text with word-level timestamps",
    level: "L2",
    files: ["src/apps/api/app/pipeline/transcribe.py"],
    githubLabel: "module:processing",
    dependsOn: ["probe"],
    produces: ["transcript JSON with timestamps"],
    business: {
      userFacing: "Listens to everything said in the video and writes it down with precise timing.",
      businessImpact: "Powers captions AND hook detection. No transcript = no good clips from talking content.",
      metric: "Transcript accuracy, processing time",
      status: "live",
    },
  },
  scene_detect: {
    id: "scene_detect",
    name: "Scene Finder",
    description: "PySceneDetect cut point detection for segment boundaries",
    level: "L2",
    files: ["src/apps/api/app/pipeline/scene_detect.py"],
    githubLabel: "module:processing",
    dependsOn: ["probe"],
    produces: ["scene cut timestamps"],
    business: {
      userFacing: "Finds where scenes change in the video. Natural cut points for clips.",
      businessImpact: "Clips that start/end at scene boundaries feel intentional, not randomly chopped.",
      metric: "Scene detection accuracy vs manual cuts",
      status: "live",
    },
  },
  score: {
    id: "score",
    name: "Clip Ranking",
    description: "Hook strength + engagement scoring, top-9 candidate selection",
    level: "L2",
    files: ["src/apps/api/app/pipeline/score.py"],
    githubLabel: "module:processing",
    dependsOn: ["transcribe", "scene_detect"],
    produces: ["top 9 ranked candidates"],
    business: {
      userFacing: "Rates every possible clip on two things: how strong is the opening hook, and how engaging is the content.",
      businessImpact: "This IS the product. If scoring picks bad clips, the whole value prop fails.",
      metric: "Clip quality vs human-rated baseline (target: top-3 overlap >60%)",
      status: "live",
    },
  },
  gemini: {
    id: "gemini",
    name: "AI Brain",
    description: "Gemini AI analysis for hook scoring, creative direction, recipes",
    level: "L2",
    files: [
      "src/apps/api/app/pipeline/agents/gemini_analyzer.py",
    ],
    githubLabel: "module:processing",
    dependsOn: ["probe"],
    produces: ["analysis JSON", "hook scores"],
    business: {
      userFacing: "Google's Gemini AI watches the video and understands what's happening: mood, energy, key moments.",
      businessImpact: "Powers creative decisions. Template matching, hook detection, and copy writing all depend on this.",
      metric: "Gemini API cost per job, analysis quality",
      status: "live",
    },
  },
};

const clipsChildren: Record<string, Module> = {
  reframe: {
    id: "reframe",
    name: "Vertical Format",
    description: "9:16 aspect ratio scaling + H.264/AAC export via FFmpeg",
    level: "L2",
    files: ["src/apps/api/app/pipeline/reframe.py"],
    githubLabel: "module:clips",
    dependsOn: ["score"],
    produces: ["1080x1920 H.264 video file"],
    business: {
      userFacing: "Converts any video to vertical 9:16 format for TikTok, Reels, and Shorts.",
      businessImpact: "Platform requirement. Wrong aspect ratio = video looks terrible or gets rejected.",
      metric: "Output quality (no black bars, face centered)",
      status: "live",
    },
  },
  captions: {
    id: "captions",
    name: "Auto-Captions",
    description: "ASS subtitle generation from Whisper timestamps",
    level: "L2",
    files: [
      "src/apps/api/app/pipeline/captions.py",
      "src/apps/api/app/pipeline/ass_utils.py",
    ],
    githubLabel: "module:clips",
    dependsOn: ["transcribe"],
    produces: ["ASS subtitle file"],
    business: {
      userFacing: "Automatically adds captions synced to speech. 80% of TikTok is watched on mute.",
      businessImpact: "Captions = 2x engagement on muted feeds. This is table stakes for short-form.",
      metric: "Caption sync accuracy, style quality",
      status: "live",
    },
  },
  text_overlay: {
    id: "text_overlay",
    name: "Text Effects",
    description: "Render captions and animated text onto video frames",
    level: "L2",
    files: ["src/apps/api/app/pipeline/text_overlay.py"],
    githubLabel: "module:clips",
    dependsOn: ["captions", "reframe"],
    produces: ["video with burned-in captions"],
    business: {
      userFacing: "Burns text directly into the video with animated styles and font cycling.",
      businessImpact: "Polished text overlays make clips look professionally edited, not auto-generated.",
      metric: "Visual quality rating",
      status: "live",
    },
  },
  thumbnail: {
    id: "thumbnail",
    name: "Cover Image",
    description: "Extract best keyframe for video thumbnail",
    level: "L2",
    files: ["src/apps/api/app/pipeline/thumbnail.py"],
    githubLabel: "module:clips",
    dependsOn: ["reframe"],
    produces: ["thumbnail image"],
    business: {
      userFacing: "Picks the best frame from the clip as the cover image for each platform.",
      businessImpact: "Good thumbnail = higher click-through rate. Bad thumbnail = scroll past.",
      metric: "Face detection rate, sharpness score",
      status: "live",
    },
  },
  copy_writer: {
    id: "copy_writer",
    name: "Caption Writer",
    description: "Platform-specific captions and hashtags via Gemini",
    level: "L2",
    files: ["src/apps/api/app/pipeline/agents/copy_writer.py"],
    githubLabel: "module:clips",
    dependsOn: ["transcribe", "score"],
    produces: ["platform_copy JSON (TikTok, IG, YouTube)"],
    business: {
      userFacing: "Writes the post caption, hashtags, and hook text for each platform automatically.",
      businessImpact: "This is where 'zero decisions' becomes real. Creator doesn't write a single word.",
      metric: "% of users who post without editing copy (target: >70%)",
      status: "live",
    },
  },
};

const templateChildren: Record<string, Module> = {
  template_matcher: {
    id: "template_matcher",
    name: "Clip-to-Template Matcher",
    description: "Match uploaded clips to template slots by energy and duration",
    level: "L2",
    files: ["src/apps/api/app/pipeline/template_matcher.py"],
    githubLabel: "module:templates",
    dependsOn: ["gemini"],
    produces: ["assembly_plan JSON"],
    business: {
      userFacing: "Figures out which of your clips fits best in each slot of the template.",
      businessImpact: "Bad matching = clips feel random. Good matching = feels like a pro edit.",
      metric: "Energy match score, duration fit accuracy",
      status: "live",
    },
  },
  transitions: {
    id: "transitions",
    name: "Smooth Transitions",
    description: "FFmpeg transition filter chains between clips",
    level: "L2",
    files: ["src/apps/api/app/pipeline/transitions.py"],
    githubLabel: "module:templates",
    dependsOn: ["template_matcher"],
    produces: ["FFmpeg filter graph"],
    business: {
      userFacing: "Adds smooth transitions between clips: crossfades, wipes, zoom cuts.",
      businessImpact: "Transitions are what make templates feel cinematic vs. amateur hard-cuts.",
      metric: "Transition rendering success rate",
      status: "live",
    },
  },
  compositor: {
    id: "compositor",
    name: "Final Assembly",
    description: "Assemble matched clips into final template video with effects",
    level: "L2",
    files: ["src/apps/api/app/tasks/template_orchestrate.py"],
    githubLabel: "module:templates",
    dependsOn: ["template_matcher", "transitions"],
    produces: ["final assembled video"],
    business: {
      userFacing: "Stitches all your clips together into one polished video matching the template style.",
      businessImpact: "The final output. If this looks good, the creator posts. If not, they leave.",
      metric: "Assembly success rate, output quality rating",
      status: "live",
    },
  },
};

const deliveryChildren: Record<string, Module> = {
  results_page: {
    id: "results_page",
    name: "Results Screen",
    description: "Clip cards with scores, thumbnails, download links, and copy",
    level: "L2",
    files: [
      "src/apps/web/src/app/jobs/[id]/page.tsx",
      "src/apps/web/src/app/template-jobs/[id]/page.tsx",
    ],
    githubLabel: "module:delivery",
    dependsOn: ["reframe", "copy_writer", "compositor"],
    produces: ["rendered clip cards in browser"],
    business: {
      userFacing: "Where the creator sees their 3 ranked clips with previews, captions, and a Post button.",
      businessImpact: "Decision moment. This screen either converts to a post or gets abandoned.",
      metric: "Time on results page, click-to-post rate",
      status: "live",
    },
  },
  posting: {
    id: "posting",
    name: "1-Click Posting",
    description: "Instagram, YouTube, TikTok upload integrations (Phase 2)",
    level: "L2",
    files: [],
    githubLabel: "module:delivery",
    dependsOn: ["results_page"],
    produces: ["posted content on platforms"],
    business: {
      userFacing: "One button posts the video to Instagram, YouTube, and TikTok simultaneously.",
      businessImpact: "THE differentiator. This is why Nova exists. No other tool closes the loop to 'posted'.",
      metric: "Posting success rate, platforms per post",
      status: "planned",
    },
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
      "src/apps/web/src/app/page.tsx",
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
    files: ["src/apps/api/app/worker.py"],
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
  importing: { moduleId: "upload", visual: "pulse" },
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
