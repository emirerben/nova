"use client";

import { useEffect, useId, useMemo, useState } from "react";
import { Check, ChevronDown, CircleAlert, Info, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  clearCreationThreadDirectionOverride,
  setCreationThreadDirectionOverride,
  type CreatorDirectionReceipt,
  type CreatorDirectionReceiptRule,
  type CreatorDirectionReceiptStatus,
} from "@/lib/creation-thread-api";
import { cn } from "@/lib/cn";
import { FONT_REGISTRY } from "@/lib/overlay-constants";

const STATUS_COPY: Record<CreatorDirectionReceiptStatus, { label: string; tone: string }> = {
  enforced: { label: "Enforced", tone: "text-lime-700" },
  advisory: { label: "Advisory", tone: "text-[#71717a]" },
  unsupported: { label: "Not supported", tone: "text-[#71717a]" },
  conflicted: { label: "Conflict", tone: "text-[#3f3f46]" },
};

function ruleStatus(rule: CreatorDirectionReceiptRule): CreatorDirectionReceiptStatus {
  const candidate = rule.status ?? rule.enforcement_status;
  return candidate && candidate in STATUS_COPY ? candidate : "advisory";
}

function ruleLabel(rule: CreatorDirectionReceiptRule): string {
  return rule.label?.trim() || rule.display_text?.trim() || rule.instruction?.trim() || rule.normalized_key?.replaceAll("_", " ") || "Preference";
}

function rulesFor(receipt: CreatorDirectionReceipt): CreatorDirectionReceiptRule[] {
  if (receipt.rules?.length) return receipt.rules;
  if (receipt.applied_rules?.length) return receipt.applied_rules;
  return receipt.items ?? receipt.rules ?? receipt.applied_rules ?? [];
}

function summaryParts(receipt: CreatorDirectionReceipt): string[] {
  return [
    receipt.enforced_count ? `${receipt.enforced_count} enforced` : null,
    receipt.advisory_count ? `${receipt.advisory_count} advisory` : null,
    receipt.unsupported_count ? `${receipt.unsupported_count} unsupported` : null,
    receipt.conflicted_count ? `${receipt.conflicted_count} conflicted` : null,
  ].filter((value): value is string => Boolean(value));
}

function projectStructuredValue(
  normalizedKey: string,
  instruction: string,
): Record<string, unknown> | undefined {
  const value = instruction.trim();
  if (normalizedKey === "font_family") {
    const candidate = value
      .replace(/^(always\s+)?use\s+(the\s+)?/i, "")
      .replace(/^(the\s+)?font(?:\s+family)?\s*(?:should\s+be|is|:)?\s*/i, "")
      .replace(/\s+font$/i, "")
      .trim();
    const key = Object.keys(FONT_REGISTRY).find((font) => font.toLocaleLowerCase() === candidate.toLocaleLowerCase());
    if (key) return { font_family: key };
  }
  if (normalizedKey === "shadow_enabled") {
    const lowered = value.toLocaleLowerCase();
    if (/\b(no|never|without|off|disable)\b/.test(lowered)) {
      return { shadow_enabled: false };
    }
    if (/\b(yes|with|on|enable|use)\b/.test(lowered)) {
      return { shadow_enabled: true };
    }
  }
  return undefined;
}

export function CreatorDirectionReceiptView({
  receipt,
  projectId,
  expectedRevision,
  className,
}: {
  receipt: CreatorDirectionReceipt | null | undefined;
  /** Creation-thread id. Omit on legacy item responses that cannot save overrides. */
  projectId?: string | null;
  /** Revision required by the compare-and-set override mutation. */
  expectedRevision?: number | null;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [localOverrides, setLocalOverrides] = useState<Record<string, boolean>>({});
  const [localReceipt, setLocalReceipt] = useState(receipt);
  const [localRevision, setLocalRevision] = useState(
    receipt?.memory_revision ?? expectedRevision ?? null,
  );
  const receiptId = useId();

  useEffect(() => {
    setLocalReceipt(receipt);
    setLocalRevision(receipt?.memory_revision ?? expectedRevision ?? null);
    setLocalOverrides({});
  }, [expectedRevision, receipt]);

  const rules = useMemo(() => (localReceipt ? rulesFor(localReceipt) : []), [localReceipt]);
  if (!localReceipt) return null;
  if (!localReceipt.enabled) {
    return <p className={cn("border-t border-zinc-200 py-3 text-xs text-muted-foreground", className)}>Personalization is paused.</p>;
  }
  if (localReceipt.applied_count < 1) {
    return <p className={cn("border-t border-zinc-200 py-3 text-xs text-muted-foreground", className)}>No saved preferences were used for this project.</p>;
  }

  const summary = summaryParts(localReceipt);
  const getRuleKey = (rule: CreatorDirectionReceiptRule, index: number) =>
    rule.normalized_key?.trim() || rule.id?.trim() || `rule-${index}`;

  async function saveOverride(rule: CreatorDirectionReceiptRule, key: string) {
    if (!projectId || localRevision == null || !rule.normalized_key || !draft.trim() || busyKey) return;
    setBusyKey(key);
    setError(null);
    try {
      const result = await setCreationThreadDirectionOverride(projectId, {
        normalized_key: rule.normalized_key,
        instruction: draft.trim(),
        structured_value: projectStructuredValue(rule.normalized_key, draft),
        expected_revision: localRevision,
      });
      setLocalRevision(result?.revision ?? localRevision + 1);
      if (result?.direction_receipt) setLocalReceipt(result.direction_receipt);
      setLocalOverrides((current) => ({ ...current, [key]: true }));
      setEditingKey(null);
      setDraft("");
    } catch {
      setError("This project-only preference couldn’t be saved. Try again.");
    } finally {
      setBusyKey(null);
    }
  }

  async function clearOverride(rule: CreatorDirectionReceiptRule, key: string) {
    if (!projectId || localRevision == null || !rule.normalized_key || busyKey) return;
    setBusyKey(key);
    setError(null);
    try {
      const result = await clearCreationThreadDirectionOverride(projectId, rule.normalized_key, localRevision);
      setLocalRevision(result?.revision ?? localRevision + 1);
      if (result?.direction_receipt) setLocalReceipt(result.direction_receipt);
      setLocalOverrides((current) => ({ ...current, [key]: false }));
    } catch {
      setError("This project-only preference couldn’t be removed. Try again.");
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <section className={cn("border-t border-zinc-200 py-3", className)} aria-label="Applied personalization">
      <Button
        type="button"
        variant="ghost"
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-none p-0 text-left text-xs text-muted-foreground transition-colors hover:bg-transparent hover:text-foreground focus-visible:ring-2 focus-visible:ring-lime-600/60 focus-visible:ring-offset-2"
        aria-expanded={expanded}
        aria-controls={receiptId}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="min-w-0 truncate">
          Personalization · {localReceipt.applied_count} applied{summary.length ? ` · ${summary.join(" · ")}` : ""}
        </span>
        <ChevronDown aria-hidden="true" className={cn("size-4 shrink-0 transition-transform motion-reduce:transition-none", expanded && "rotate-180")} />
      </Button>
      {expanded && (
        <div id={receiptId} className="space-y-3 pb-1 pt-2" role="region" aria-label="Applied personalization details">
          <p className="text-xs leading-relaxed text-[#71717a]">
            These preferences shaped this project. Project-only changes stay here and do not change Personalization.
          </p>
          {rules.length ? (
            <ul className="divide-y divide-zinc-200" aria-label="Applied preference rules">
              {rules.map((rule, index) => {
                const key = getRuleKey(rule, index);
                const status = ruleStatus(rule);
                const copy = STATUS_COPY[status];
                const hasOverride = Boolean(rule.overridden || localOverrides[key]);
                const canOverride = Boolean(projectId && localRevision != null && rule.normalized_key);
                return (
                  <li key={key} className="py-3 first:pt-1 last:pb-1">
                    <div className="flex items-start gap-2">
                      {status === "enforced" ? <Check aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-lime-700" /> : status === "conflicted" || status === "unsupported" ? <CircleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-[#71717a]" /> : <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-[#71717a]" />}
                      <div className="min-w-0 flex-1">
                        <p className="text-pretty text-sm leading-relaxed text-[#3f3f46]">
                          {ruleLabel(rule)}
                        </p>
                        <p className={cn("mt-1 text-xs", copy.tone)}>{copy.label}{rule.scope_label ? ` · ${rule.scope_label}` : ""}</p>
                        {(rule.reason || rule.conflict_message) && (
                          <p className="mt-1 text-pretty text-xs leading-relaxed text-[#71717a]">
                            {rule.reason || rule.conflict_message}
                          </p>
                        )}
                        {canOverride && (
                          <div className="mt-2">
                            {editingKey === key ? (
                              <div className="space-y-2">
                                <Input value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={500} placeholder="Only for this project…" aria-label={`Project-only preference for ${ruleLabel(rule)}`} autoFocus />
                                <div className="flex flex-wrap gap-2">
                                  <Button type="button" size="sm" variant="ink" onClick={() => void saveOverride(rule, key)} disabled={!draft.trim() || busyKey === key}>{busyKey === key ? "Saving…" : "Save for this project"}</Button>
                                  <Button type="button" size="sm" variant="outline" onClick={() => { setEditingKey(null); setDraft(""); }} disabled={busyKey === key}>Cancel</Button>
                                </div>
                              </div>
                            ) : hasOverride ? (
                              <Button type="button" variant="link" className="min-h-11 p-0 text-xs" onClick={() => void clearOverride(rule, key)} disabled={busyKey === key}><RotateCcw aria-hidden="true" className="mr-1 size-3" />Use account preference</Button>
                            ) : (
                              <Button type="button" variant="link" className="min-h-11 p-0 text-xs" onClick={() => { setEditingKey(key); setDraft(""); }}>Override for this project</Button>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-xs text-[#71717a]">Detailed rule status will appear here as this project receives its next update.</p>
          )}
          {error && <p className="text-xs text-red-700" role="alert">{error}</p>}
        </div>
      )}
    </section>
  );
}

export default CreatorDirectionReceiptView;
