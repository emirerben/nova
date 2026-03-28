import useSWR from "swr";
import {
  getJobStatus,
  getTemplateJobStatus,
  listTemplateJobs,
  TERMINAL_STATES,
  type JobStatusResponse,
  type TemplateJobStatusResponse,
} from "@/lib/api";
import { JOB_STATUS_MODULE_MAP } from "@/lib/architecture-config";

// ---------------------------------------------------------------------------
// GitHub data hooks
// ---------------------------------------------------------------------------

interface GitHubIssue {
  title: string;
  url: string;
  number: number;
  state: string;
  created_at: string;
}

interface GitHubCommit {
  sha: string;
  message: string;
  author: string;
  date: string;
  url: string;
}

interface GitHubResponse<T> {
  items: T[];
  rateLimited?: boolean;
}

const githubFetcher = async <T>(url: string): Promise<GitHubResponse<T>> => {
  const res = await fetch(url);
  if (!res.ok) return { items: [], rateLimited: res.status === 403 };
  return res.json();
};

/** Fetch open issues for a module label (e.g., "module:processing") */
export function useModuleIssues(label: string | null) {
  return useSWR<GitHubResponse<GitHubIssue>>(
    label ? `/api/architecture/github?type=issues&label=${encodeURIComponent(label)}` : null,
    githubFetcher,
    { refreshInterval: 60_000, revalidateOnFocus: false }
  );
}

/** Fetch recent commits for a module directory path */
export function useModuleCommits(path: string | null) {
  return useSWR<GitHubResponse<GitHubCommit>>(
    path ? `/api/architecture/github?type=commits&path=${encodeURIComponent(path)}` : null,
    githubFetcher,
    { refreshInterval: 60_000, revalidateOnFocus: false }
  );
}

// ---------------------------------------------------------------------------
// Active jobs hook (localStorage + polling)
// ---------------------------------------------------------------------------

const STORAGE_KEY = "nova_recent_jobs";
const TEMPLATE_STORAGE_KEY = "nova_recent_template_jobs";
const MAX_RECENT_JOBS = 10;

interface ActiveJob {
  jobId: string;
  status: string;
  moduleId: string;
  visual: "pulse" | "check" | "check-yellow" | "error";
  type: "default" | "template";
}

function readRecentJobIds(key: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const ids = JSON.parse(raw);
    if (!Array.isArray(ids)) return [];
    return ids.slice(0, MAX_RECENT_JOBS).filter((id: unknown) => typeof id === "string");
  } catch {
    return [];
  }
}

/** Save a job ID to localStorage for live activity tracking */
export function trackRecentJob(jobId: string, type: "default" | "template" = "default") {
  if (typeof window === "undefined") return;
  const key = type === "template" ? TEMPLATE_STORAGE_KEY : STORAGE_KEY;
  const existing = readRecentJobIds(key);
  if (existing.includes(jobId)) return;
  const updated = [jobId, ...existing].slice(0, MAX_RECENT_JOBS);
  localStorage.setItem(key, JSON.stringify(updated));
}

function pruneTerminalJobs(key: string, terminalIds: string[]) {
  if (typeof window === "undefined" || terminalIds.length === 0) return;
  const existing = readRecentJobIds(key);
  const filtered = existing.filter((id) => !terminalIds.includes(id));
  localStorage.setItem(key, JSON.stringify(filtered));
}

async function fetchActiveDefaultJobs(): Promise<ActiveJob[]> {
  const ids = readRecentJobIds(STORAGE_KEY);
  if (ids.length === 0) return [];

  const results = await Promise.allSettled(ids.map((id) => getJobStatus(id)));
  const terminalIds: string[] = [];
  const active: ActiveJob[] = [];

  results.forEach((result, i) => {
    if (result.status !== "fulfilled") return;
    const job: JobStatusResponse = result.value;
    const mapping = JOB_STATUS_MODULE_MAP[job.status];
    if (!mapping) return;

    if (TERMINAL_STATES.has(job.status as Parameters<typeof TERMINAL_STATES.has>[0])) {
      terminalIds.push(ids[i]);
    } else {
      active.push({
        jobId: job.id,
        status: job.status,
        moduleId: mapping.moduleId,
        visual: mapping.visual,
        type: "default",
      });
    }
  });

  pruneTerminalJobs(STORAGE_KEY, terminalIds);
  return active;
}

async function fetchActiveTemplateJobs(): Promise<ActiveJob[]> {
  try {
    const { jobs } = await listTemplateJobs(10, 0);
    const active: ActiveJob[] = [];

    for (const job of jobs) {
      const mapping = JOB_STATUS_MODULE_MAP[job.status];
      if (!mapping) continue;
      if (job.status !== "queued" && job.status !== "processing") continue;

      active.push({
        jobId: job.job_id,
        status: job.status,
        moduleId: mapping.moduleId,
        visual: mapping.visual,
        type: "template",
      });
    }

    return active;
  } catch {
    return [];
  }
}

async function fetchAllActiveJobs(): Promise<ActiveJob[]> {
  const [defaultJobs, templateJobs] = await Promise.all([
    fetchActiveDefaultJobs(),
    fetchActiveTemplateJobs(),
  ]);
  return [...defaultJobs, ...templateJobs];
}

/** Poll for active jobs across both default and template modes */
export function useActiveJobs() {
  return useSWR<ActiveJob[]>("active-jobs", fetchAllActiveJobs, {
    refreshInterval: 5_000,
    revalidateOnFocus: false,
  });
}
