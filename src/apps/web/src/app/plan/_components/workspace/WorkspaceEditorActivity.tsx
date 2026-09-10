"use client";

import { StableVideo } from "@/components/StableVideo";
import { hedgedReason } from "@/app/plan/items/[id]/_editor/OverlaySuggestions";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { AgentApprovalCard } from "@/components/chat/AgentApprovalCard";
import { NovaActivityFeed } from "@/components/progress";
import DirectorSuggestions from "@/app/plan/items/[id]/_editor/DirectorSuggestions";
import type { EditorChatAction, EditorChatState } from "@/lib/editor-chat/protocol";

function formatTime(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** Editor AI controls share the main transcript; there is no second composer. */
export default function WorkspaceEditorActivity({ state, onCommand }: {
  state: EditorChatState | null;
  onCommand: (action: EditorChatAction) => void;
}) {
  if (!state) return null;
  const review = state.director;
  const lastChange = [...state.messages].reverse().find((message) => message.applied?.length);
  return <div className="space-y-4" aria-label="Editor actions">
    {state.sending && !state.confirmation ? <Button type="button" variant="outline" onClick={() => onCommand({ kind: "stop" })}>Stop editing request</Button> : null}
    {state.confirmation ? <AgentApprovalCard
      badge={<Badge variant="secondary">Render required</Badge>}
      title={state.confirmation.title}
      description="This processes a new video version. Your current version remains available."
      actions={<div className="flex flex-wrap gap-2">
        <Button type="button" onClick={() => onCommand({ kind: "confirm", confirmationId: state.confirmation!.id, approved: true })}>Confirm and render</Button>
        <Button type="button" variant="outline" onClick={() => onCommand({ kind: "confirm", confirmationId: state.confirmation!.id, approved: false })}>Not now</Button>
      </div>}
    /> : null}
    {state.renderActive ? <div role="status"><p className="text-sm text-muted-foreground">Rendering the new version…</p>
      {state.renderSteps?.length ? <NovaActivityFeed steps={state.renderSteps} isTerminal={false} isSuccess={false} /> : null}
    </div> : null}
    {lastChange?.undoVersion === state.historyVersion && state.canUndo && !state.sending && !state.readOnly
      ? <Button type="button" variant="outline" onClick={() => onCommand({ kind: "undo" })}>Undo last edit</Button> : null}
    {state.error ? <p role="alert" className="text-sm text-destructive">{state.error}</p> : null}
    {state.visuals ? <section aria-label="Suggested visuals" className="space-y-2">
      <p className="text-sm font-medium">Suggested visuals</p>
      {state.visuals.staleNotice ? <p className="text-sm text-muted-foreground">Your script changed. Match visuals again.</p> : null}
      {state.visuals.phase === "matching" ? <p role="status" className="text-sm text-muted-foreground">Matching visuals to your script…{state.visuals.stillWorking ? " Still working…" : ""}</p> : null}
      {state.visuals.phase === "failed" ? <p role="alert" className="text-sm text-destructive">Visual matching failed. Try again.</p> : null}
      {state.visuals.phase === "zero" ? <p className="text-sm text-muted-foreground">No confident matches. Add more specific visuals in the editor.</p> : null}
      {state.visuals.rows.map((row) => {
        const asset = state.visuals?.assets?.find((candidate) => candidate.id === row.asset_id);
        const label = asset?.source_filename ?? asset?.subject ?? row.overlay.kind;
        const preview = row.overlay.preview_url ?? asset?.display_url;
        return <div key={row.id} className="rounded-lg border p-3 text-sm">
        <div className="mb-2 flex items-start gap-2">
          {preview ? <div className="h-12 w-16 shrink-0 overflow-hidden rounded-md border bg-muted">
            {row.overlay.kind === "video" ? <StableVideo src={preview} muted playsInline preload="metadata" aria-label={`Preview of ${label}`} className="h-full w-full object-cover" /> : (
              // eslint-disable-next-line @next/next/no-img-element -- signed asset preview
              <img src={preview} alt={`Preview of ${label}`} className="h-full w-full object-cover" />
            )}
          </div> : null}
          <div className="min-w-0"><p className="break-words font-medium">{label}</p><p className="text-muted-foreground">{formatTime(row.overlay.start_s)}–{formatTime(row.overlay.end_s)}</p></div>
        </div>
        <p>{hedgedReason(row)}</p>
        {row.sfx ? <p className="mt-1 text-muted-foreground">Includes {row.sfx.label ?? "pop"} sound</p> : null}
        <div className="mt-2 flex flex-wrap gap-2">
          <Button variant="outline" disabled={state.sending || state.saving || state.readOnly} onClick={() => onCommand({ kind: "visuals-accept", suggestionId: row.id })}>Add visual</Button>
          <Button variant="ghost" onClick={() => onCommand({ kind: "visuals-reveal", suggestionId: row.id })}>Show moment</Button>
          <Button variant="ghost" onClick={() => onCommand({ kind: "visuals-dismiss", suggestionId: row.id })}>Dismiss</Button>
        </div>
      </div>; })}
      {state.visuals.wishlist.map((text) => <p key={text} className="text-sm text-muted-foreground">{text}</p>)}
      <Button variant="outline" disabled={state.sending || state.saving || state.readOnly || state.visuals.unavailable || state.visuals.phase === "matching" || state.visuals.readyAssets === 0} onClick={() => onCommand({ kind: "visuals-start" })}>Place visuals automatically</Button>
      {state.visuals.readyAssets === 0 ? <p className="text-sm text-muted-foreground">Add visuals in the editor to match them to your script.</p> : null}
    </section> : null}
    {review ? <DirectorSuggestions
      {...review}
      historyVersion={state.historyVersion}
      loading={review.loading || state.sending || Boolean(state.confirmation) || state.saving}
      reviewBlocked={review.reviewBlocked || review.unavailable || state.readOnly}
      onRefresh={() => onCommand({ kind: "review" })}
      onAccept={(suggestion, options) => onCommand({ kind: "accept", suggestionId: suggestion.id, omniCostConfirmed: options?.omniCostConfirmed })}
      onDismiss={(suggestion) => onCommand({ kind: "dismiss", suggestionId: suggestion.id })}
      onRevealApplied={(receipt) => onCommand({ kind: "reveal", receiptId: receipt.id })}
      onCancelGeneration={() => onCommand({ kind: "cancel-generation" })}
      onRestoreOriginalTiming={() => onCommand({ kind: "restore-timing" })}
    /> : null}
  </div>;
}
