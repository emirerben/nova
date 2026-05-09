import useSWR from "swr";
import { listTemplateJobs } from "@/lib/api";
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
export function trackRecentJob(jobId: string, _type: "default" | "template" = "template") {
  if (typeof window === "undefined") return;
  const key = TEMPLATE_STORAGE_KEY;
  const existing = readRecentJobIds(key);
  if (existing.includes(jobId)) return;
  const updated = [jobId, ...existing].slice(0, MAX_RECENT_JOBS);
  localStorage.setItem(key, JSON.stringify(updated));
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

/** Poll for active template jobs (the only job type post-cleanup). */
export function useActiveJobs() {
  return useSWR<ActiveJob[]>("active-jobs", fetchActiveTemplateJobs, {
    refreshInterval: 5_000,
    revalidateOnFocus: false,
  });
}
