"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  disconnectTikTok,
  getTikTokConnection,
  startTikTokOAuth,
  syncTikTok,
  type TikTokConnection,
} from "@/lib/tiktok-api";

const REQUIRED_SCOPES = ["user.info.basic", "video.publish", "video.upload"];

export default function TikTokConnectionPanel() {
  const [connection, setConnection] = useState<TikTokConnection | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disconnectOpen, setDisconnectOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setConnection(await getTikTokConnection());
      setError(null);
    } catch {
      setConnection(null);
      setError("Kria couldn’t load your TikTok connection. Try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      await startTikTokOAuth("/plan/tiktok");
    } catch {
      setError("TikTok access couldn’t start. Try again.");
      setBusy(false);
    }
  }

  async function disconnect() {
    setDisconnectOpen(false);
    setBusy(true);
    setError(null);
    try {
      await disconnectTikTok();
      await load();
    } catch {
      setError("Kria couldn’t disconnect TikTok. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function sync() {
    setBusy(true);
    setError(null);
    try {
      await syncTikTok();
      await load();
    } catch {
      setError("Kria couldn’t sync TikTok performance. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const missingScopes = connection
    ? REQUIRED_SCOPES.filter((scope) => !connection.granted_scopes.includes(scope))
    : [];
  const reconnectRequired = connection?.status === "reconnect_required";
  const partialGrant = Boolean(connection?.connected && missingScopes.length > 0);
  const showConnect = Boolean(connection && (!connection.connected || reconnectRequired || partialGrant));

  return (
    <Card id="tiktok" role="region" aria-label="TikTok connection">
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <CardTitle className="font-display text-2xl">TikTok</CardTitle>
            <CardDescription>
              Connect your account to publish an approved Kria edit or send it to your TikTok inbox.
            </CardDescription>
          </div>
          {connection?.connected && !reconnectRequired && !partialGrant ? (
            <Badge variant="secondary">Connected</Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? <p className="text-sm text-muted-foreground" role="status">Checking TikTok connection…</p> : null}
        {!loading && !connection ? (
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-sm text-muted-foreground" role="alert">{error ?? "TikTok connection is unavailable."}</p>
            <Button type="button" variant="outline" className="min-h-11" onClick={() => void load()}>Retry</Button>
          </div>
        ) : null}
        {!loading && connection && !connection.available ? (
          <p className="text-sm text-muted-foreground">TikTok connection is not available for this account yet.</p>
        ) : null}
        {!loading && connection?.available ? (
          <>
            <p className="text-sm text-muted-foreground">
              {showConnect
                ? connection.connected
                  ? "TikTok access needs to be reconnected before publishing."
                  : "Connect TikTok before publishing from Kria."
                : connection.account?.display_name
                  ? `Connected as ${connection.account.display_name}.`
                  : "Your TikTok account is connected."}
            </p>
            {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
            <div className="flex flex-wrap gap-2">
              {showConnect ? (
                <Button type="button" className="min-h-11" disabled={busy} onClick={() => void connect()}>
                  {busy ? "Connecting…" : connection.connected ? "Reconnect TikTok" : "Connect TikTok"}
                </Button>
              ) : (
                <>
                  {connection.can_analyze ? <Button type="button" variant="outline" className="min-h-11" disabled={busy} onClick={() => void sync()}>Sync performance</Button> : null}
                  <Button type="button" variant="outline" className="min-h-11" disabled={busy} onClick={() => setDisconnectOpen(true)}>Disconnect</Button>
                </>
              )}
            </div>
            {partialGrant ? <p className="text-xs text-muted-foreground">Some TikTok permissions are missing; reconnect to restore publishing access.</p> : null}
            {connection.last_synced_at ? <p className="text-xs text-muted-foreground">Last synced {formatSyncedAgo(connection.last_synced_at)}.</p> : null}
          </>
        ) : null}
      </CardContent>
      <AlertDialog open={disconnectOpen} onOpenChange={setDisconnectOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect TikTok?</AlertDialogTitle>
            <AlertDialogDescription>Removes TikTok access from Kria. Your videos remain in Kria and on TikTok.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="min-h-11">Cancel</AlertDialogCancel>
            <AlertDialogAction className="min-h-11" onClick={() => void disconnect()}>Disconnect</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

function formatSyncedAgo(value: string): string {
  const elapsedMs = Math.max(0, Date.now() - new Date(value).getTime());
  const mins = Math.round(elapsedMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
