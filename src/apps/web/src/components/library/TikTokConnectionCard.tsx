"use client";

import { useEffect, useState } from "react";
import { MoreHorizontal } from "lucide-react";
import {
  disconnectTikTok,
  getTikTokConnection,
  startTikTokOAuth,
  syncTikTok,
  type TikTokConnection,
} from "@/lib/tiktok-api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * Integrations row (DESIGN.md §15 / §12, Paper "P1 Home" + "C3 Cards, media
 * & lists"): brand glyph · name + status Badge · one-line meta · a primary
 * Connect/Reconnect action or an overflow menu (Sync performance /
 * Disconnect, the latter behind an AlertDialog — never `window.confirm`).
 */
export default function TikTokConnectionCard({ onConnection }: { onConnection?: (value: TikTokConnection | null) => void }) {
  const [connection, setConnection] = useState<TikTokConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDisconnectOpen, setConfirmDisconnectOpen] = useState(false);

  async function load() {
    try {
      const value = await getTikTokConnection();
      setConnection(value);
      onConnection?.(value);
    } catch {
      setConnection(null);
      onConnection?.(null);
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  if (!connection?.available) return null;
  const missingScopes = ["user.info.basic", "video.publish", "video.upload"].filter(
    (scope) => !connection.granted_scopes.includes(scope),
  );
  const partialGrant = connection.connected && missingScopes.length > 0;
  const reconnectRequired = connection.status === "reconnect_required";

  async function connect() {
    setBusy(true); setError(null);
    try { await startTikTokOAuth(); } catch (reason) { setError(tiktokConnectionError("connect to TikTok", reason)); setBusy(false); }
  }
  async function disconnect() {
    setConfirmDisconnectOpen(false);
    setBusy(true); setError(null);
    try { await disconnectTikTok(); await load(); } catch (reason) { setError(tiktokConnectionError("disconnect TikTok", reason)); }
    finally { setBusy(false); }
  }
  async function sync() {
    setBusy(true); setError(null);
    try { await syncTikTok(); } catch (reason) { setError(tiktokConnectionError("sync TikTok performance", reason)); }
    finally { setBusy(false); }
  }

  const showConnect = !connection.connected || reconnectRequired || partialGrant;
  const displayName = connection.account?.display_name || "TikTok";
  const meta = connection.connected
    ? [displayName !== "TikTok" ? displayName : null, connection.last_synced_at ? `Last synced ${formatSyncedAgo(connection.last_synced_at)}` : null]
        .filter(Boolean)
        .join(" · ") || "Connected"
    : "Publish directly from Kria";

  return (
    <section className="rounded-2xl border border-zinc-200 bg-white p-4" aria-label="TikTok connection">
      <div className="flex items-center gap-3">
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[12px] bg-[#0c0c0e] text-white">
          <TikTokGlyph />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-base text-[#0c0c0e]">TikTok</span>
            {connection.connected && !reconnectRequired && !partialGrant && (
              !connection.audited ? (
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span>
                        <Badge variant="lime-soft" className="normal-case tracking-normal">Connected</Badge>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent>Private beta</TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              ) : (
                <Badge variant="lime-soft" className="normal-case tracking-normal">Connected</Badge>
              )
            )}
            {reconnectRequired && (
              <Badge variant="zinc" className="normal-case tracking-normal">Reconnect required</Badge>
            )}
            {!reconnectRequired && partialGrant && (
              <Badge variant="zinc" className="normal-case tracking-normal">Partial access</Badge>
            )}
          </div>
          <p className="mt-0.5 truncate text-xs text-[#71717a]">{meta}</p>
          {error && <p className="mt-1 text-xs text-[#3f3f46]">{error}</p>}
        </div>
        <div className="shrink-0">
          {showConnect ? (
            <Button variant="ink" size="sm" disabled={busy} onClick={() => void connect()}>
              {busy ? (connection.connected ? "Reconnecting…" : "Connecting…") : connection.connected ? "Reconnect" : "Connect"}
            </Button>
          ) : (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon" aria-label="More TikTok actions" disabled={busy}>
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {connection.can_analyze && (
                  <DropdownMenuItem onSelect={() => void sync()}>Sync TikTok performance</DropdownMenuItem>
                )}
                <DropdownMenuItem onSelect={() => setConfirmDisconnectOpen(true)}>Disconnect</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={confirmDisconnectOpen}
        question="Disconnect TikTok?"
        detail="Removes TikTok access from Kria. Your videos remain in Kria and on TikTok."
        confirmLabel="Disconnect"
        onConfirm={() => void disconnect()}
        onCancel={() => setConfirmDisconnectOpen(false)}
      />
    </section>
  );
}

function tiktokConnectionError(action: string, reason: unknown): string {
  const message = reason instanceof Error ? reason.message : "";
  if (/connect|reconnect|authorization|expired|permission/i.test(message)) {
    return "TikTok access needs to be reconnected before Kria can do that.";
  }
  if (/network|fetch|timeout|reach/i.test(message)) {
    return "Kria couldn't reach TikTok. Check your connection and try again.";
  }
  return `Kria couldn't ${action}. Try again.`;
}

function formatSyncedAgo(value: string): string {
  const elapsedMs = Math.max(0, Date.now() - new Date(value).getTime());
  const mins = Math.round(elapsedMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function TikTokGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor" aria-hidden="true">
      <path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z" />
    </svg>
  );
}
