"use client";

/**
 * /plan/items/[id]/edit?variant=<id> — the full-screen TikTok-parity editor
 * shell (plan §1). Thin route wrapper; all behavior lives in
 * ../_editor/EditorShell. The Suspense boundary is required by Next.js for
 * useSearchParams in a client page.
 */

import { Suspense, useEffect, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import EditorShell from "../_editor/EditorShell";

function EditPageInner() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const variantParam = search.get("variant");
  const router = useRouter();
  const [embedded, setEmbedded] = useState(false);
  const embeddedParam = search.get("embedded");
  useEffect(() => {
    if (embeddedParam === "1" && window.parent !== window) {
      setEmbedded(true);
    } else {
      const query = new URLSearchParams({ editor_item: params.id });
      if (variantParam) query.set("variant", variantParam);
      router.replace(`/plan?${query}`);
    }
  }, [embeddedParam, params.id, router, variantParam]);
  if (!embedded) return <div role="status" className="flex h-dvh items-center justify-center bg-background">Opening your project…</div>;
  // A route-param change must create a fresh editor session. Reusing the shell
  // would retain dirty working state long enough to contaminate the next
  // item's crash-recovery draft while its data loads.
  return (
    <EditorShell
      key={`${params.id}:${variantParam ?? ""}`}
      itemId={params.id}
      variantParam={variantParam}
    />
  );
}

export default function EditPage() {
  return (
    <Suspense fallback={<div className="fixed inset-0 z-50 bg-[#ffffff]" />}>
      <EditPageInner />
    </Suspense>
  );
}
