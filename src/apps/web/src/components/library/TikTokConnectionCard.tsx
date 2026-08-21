"use client";

import { useEffect, useState } from "react";
import {
  disconnectTikTok,
  getTikTokConnection,
  startTikTokOAuth,
  syncTikTok,
  type TikTokConnection,
} from "@/lib/tiktok-api";

export default function TikTokConnectionCard({ onConnection }: { onConnection?: (value: TikTokConnection | null) => void }) {
  const [connection, setConnection] = useState<TikTokConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  async function connect() {
    setBusy(true); setError(null);
    try { await startTikTokOAuth(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not connect TikTok"); setBusy(false); }
  }
  async function disconnect() {
    if (!window.confirm("Disconnect TikTok and erase the stored TikTok credentials?")) return;
    setBusy(true); setError(null);
    try { await disconnectTikTok(); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not disconnect TikTok"); }
    finally { setBusy(false); }
  }
  async function sync() {
    setBusy(true); setError(null);
    try { await syncTikTok(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not sync TikTok"); }
    finally { setBusy(false); }
  }

  return (
    <section className="mb-8 rounded-2xl border border-zinc-200 bg-white p-5" aria-label="TikTok connection">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#71717a]">TikTok</p>
          <p className="mt-1 font-display text-xl text-[#0c0c0e]">{connection.connected ? connection.account?.display_name || "Connected" : "Publish with TikTok"}</p>
          <p className="mt-1 max-w-xl text-sm text-[#71717a]">
            {connection.connected
              ? "Post an approved edit now, or send it to TikTok to finish there."
              : "Connect your account to post finalized videos or finish them in TikTok."}
          </p>
          {connection.connected && !connection.audited && <p className="mt-2 text-xs text-[#71717a]">Private beta: Direct Posts are Only you until TikTok approves public posting.</p>}
          {connection.status === "reconnect_required" && <p className="mt-2 text-xs text-red-700">TikTok access expired. Reconnect to continue.</p>}
          {partialGrant && <p className="mt-2 text-xs text-[#71717a]">TikTok granted partial access. Reconnect to enable {missingScopes.includes("video.publish") ? "Direct Post" : "draft handoff"}.</p>}
          {connection.last_synced_at && <p className="mt-2 text-xs text-[#a1a1aa]">Last synced {new Date(connection.last_synced_at).toLocaleString()}</p>}
          {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
        </div>
        <div className="flex flex-wrap gap-2">
          {!connection.connected || connection.status === "reconnect_required" ? (
            <button type="button" disabled={busy} onClick={() => void connect()} className="min-h-11 rounded-full bg-[#0c0c0e] px-5 py-2 text-sm font-semibold text-white disabled:opacity-50">{connection.status === "reconnect_required" ? "Reconnect TikTok" : "Connect TikTok"}</button>
          ) : (
            <>
              {partialGrant && <button type="button" disabled={busy} onClick={() => void connect()} className="min-h-11 rounded-full bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Reconnect</button>}
              {connection.can_analyze && <button type="button" disabled={busy} onClick={() => void sync()} className="min-h-11 rounded-full border border-zinc-200 px-4 py-2 text-sm">Sync performance</button>}
              <button type="button" disabled={busy} onClick={() => void disconnect()} className="min-h-11 rounded-full border border-zinc-200 px-4 py-2 text-sm text-[#71717a]">Disconnect</button>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
