"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import TikTokConnectionPanel from "../_components/TikTokConnectionPanel";
import { LightShell } from "../_components/ui/LightShell";

export default function TikTokConnectionPage() {
  return (
    <LightShell size="narrow">
      <Link href="/plan" className="inline-flex min-h-11 items-center text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
        <ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />
        Back to workspace
      </Link>
      <div className="mt-8 space-y-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-lime-700">Delivery</p>
        <h1 className="font-display text-3xl font-medium">Connect TikTok</h1>
        <p className="max-w-xl text-pretty text-sm leading-relaxed text-muted-foreground">
          Manage the account Kria can use when you release an approved edit.
        </p>
      </div>
      <div className="mt-8">
        <TikTokConnectionPanel />
      </div>
    </LightShell>
  );
}
