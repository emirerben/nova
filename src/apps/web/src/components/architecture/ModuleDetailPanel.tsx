"use client";

import * as Dialog from "@radix-ui/react-dialog";
import type { Module } from "@/lib/architecture-config";
import { useModuleIssues, useModuleCommits } from "@/hooks/useArchitectureData";

interface ModuleDetailPanelProps {
  module: Module | null;
  onClose: () => void;
  viewMode: "technical" | "business";
}

function FilesSection({ files }: { files: string[] }) {
  if (files.length === 0) {
    return <p className="text-xs text-gray-500 italic">No files listed</p>;
  }
  return (
    <ul className="space-y-1">
      {files.map((file) => (
        <li key={file}>
          <a
            href={`https://github.com/emirerben/nova/blob/main/${file}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs font-mono text-blue-400 hover:text-blue-300 transition-colors"
          >
            {file}
          </a>
        </li>
      ))}
    </ul>
  );
}

function CommitsSection({ modulePath }: { modulePath: string | null }) {
  const { data, isLoading } = useModuleCommits(modulePath);

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-4 bg-gray-800 rounded animate-pulse" />
        ))}
      </div>
    );
  }

  if (data?.rateLimited) {
    return <p className="text-xs text-gray-500 italic">Commits unavailable</p>;
  }

  if (!data || data.items.length === 0) {
    return <p className="text-xs text-gray-500 italic">No recent commits</p>;
  }

  return (
    <ul className="space-y-2">
      {data.items.map((commit) => (
        <li key={commit.sha} className="text-xs">
          <a
            href={commit.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-gray-300 hover:text-gray-100 transition-colors"
          >
            <span className="font-mono text-gray-500">{commit.sha}</span>{" "}
            {commit.message}
          </a>
          <div className="text-gray-500 mt-0.5">
            {commit.author} · {new Date(commit.date).toLocaleDateString()}
          </div>
        </li>
      ))}
    </ul>
  );
}

function IssuesSection({ label }: { label: string | null }) {
  const { data, isLoading } = useModuleIssues(label);

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2].map((i) => (
          <div key={i} className="h-4 bg-gray-800 rounded animate-pulse" />
        ))}
      </div>
    );
  }

  if (data?.rateLimited) {
    return <p className="text-xs text-gray-500 italic">Issues unavailable</p>;
  }

  if (!data || data.items.length === 0) {
    return (
      <p className="text-xs text-gray-500">
        No open issues <span className="text-emerald-500">✓</span>
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {data.items.map((issue) => (
        <li key={issue.number} className="text-xs">
          <a
            href={issue.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-gray-200 hover:text-white transition-colors"
          >
            #{issue.number} {issue.title}
          </a>
        </li>
      ))}
    </ul>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs uppercase tracking-wider text-gray-300 mb-2 mt-4 first:mt-0">
      {children}
    </h3>
  );
}

/** Derive a primary directory path from the module's file list for commit fetching */
function getPrimaryPath(files: string[]): string | null {
  if (files.length === 0) return null;
  // Find the most common parent directory
  const dirs = files.map((f) => f.split("/").slice(0, -1).join("/"));
  const counts = new Map<string, number>();
  for (const dir of dirs) {
    counts.set(dir, (counts.get(dir) ?? 0) + 1);
  }
  let best = dirs[0];
  let bestCount = 0;
  counts.forEach((count, dir) => {
    if (count > bestCount) {
      best = dir;
      bestCount = count;
    }
  });
  return best || null;
}

function BusinessSection({ module }: { module: Module }) {
  const biz = module.business;
  if (!biz) return <p className="text-xs text-gray-500 italic">No business context defined</p>;

  const statusColor = {
    live: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
    building: "bg-amber-500/20 text-amber-400 border-amber-500/30",
    planned: "bg-gray-700/50 text-gray-400 border-gray-600",
  }[biz.status];

  return (
    <div className="space-y-4">
      <div className={`rounded-lg border p-3 ${statusColor}`}>
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xs font-medium uppercase tracking-wider">Status</span>
          <span className="text-xs font-semibold">{biz.status}</span>
        </div>
      </div>

      <div>
        <SectionHeader>What users see</SectionHeader>
        <p className="text-sm text-gray-200 leading-relaxed">{biz.userFacing}</p>
      </div>

      <div>
        <SectionHeader>Why it matters</SectionHeader>
        <p className="text-sm text-gray-300 leading-relaxed">{biz.businessImpact}</p>
      </div>

      <div>
        <SectionHeader>Key metric</SectionHeader>
        <p className="text-xs text-gray-400 font-mono bg-gray-800 px-3 py-2 rounded">
          {biz.metric}
        </p>
      </div>
    </div>
  );
}

export function ModuleDetailPanel({ module, onClose, viewMode }: ModuleDetailPanelProps) {
  const primaryPath = module ? getPrimaryPath(module.files) : null;
  const isBusiness = viewMode === "business";

  return (
    <Dialog.Root open={!!module} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40 z-40" />
        <Dialog.Content
          className="fixed top-0 right-0 h-full w-[400px] bg-gray-900 border-l border-gray-800 z-50
                     overflow-y-auto p-5 shadow-xl
                     data-[state=open]:animate-in data-[state=open]:slide-in-from-right
                     data-[state=closed]:animate-out data-[state=closed]:slide-out-to-right"
        >
          {module && (
            <>
              <div className="flex items-start justify-between mb-4">
                <div>
                  <Dialog.Title className="text-base font-semibold text-gray-100">
                    {module.name}
                  </Dialog.Title>
                  <Dialog.Description className="text-xs text-gray-400 mt-1">
                    {isBusiness && module.business
                      ? module.business.userFacing
                      : module.description}
                  </Dialog.Description>
                </div>
                <Dialog.Close className="text-gray-500 hover:text-gray-300 transition-colors p-1">
                  ✕
                </Dialog.Close>
              </div>

              {isBusiness ? (
                <BusinessSection module={module} />
              ) : (
                <>
                  {module.produces.length > 0 && (
                    <>
                      <SectionHeader>Produces</SectionHeader>
                      <div className="flex flex-wrap gap-1.5">
                        {module.produces.map((p) => (
                          <span
                            key={p}
                            className="text-[10px] bg-gray-800 text-gray-400 px-2 py-0.5 rounded"
                          >
                            {p}
                          </span>
                        ))}
                      </div>
                    </>
                  )}

                  <SectionHeader>Files</SectionHeader>
                  <FilesSection files={module.files} />

                  <SectionHeader>Recent Commits</SectionHeader>
                  <CommitsSection modulePath={primaryPath} />
                </>
              )}

              {/* Issues show in both views */}
              <SectionHeader>Open Issues</SectionHeader>
              <IssuesSection label={module.githubLabel} />
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
