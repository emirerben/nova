"use client";

/**
 * /plan/items/[id]/edit?variant=<id> — the full-screen TikTok-parity editor
 * shell (plan §1). Thin route wrapper; all behavior lives in
 * ../_editor/EditorShell. The Suspense boundary is required by Next.js for
 * useSearchParams in a client page.
 */

import { Suspense } from "react";
import { useParams, useSearchParams } from "next/navigation";
import EditorShell from "../_editor/EditorShell";

function EditPageInner() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  return <EditorShell itemId={params.id} variantParam={search.get("variant")} />;
}

export default function EditPage() {
  return (
    <Suspense fallback={<div className="fixed inset-0 z-50 bg-[#fafaf8]" />}>
      <EditPageInner />
    </Suspense>
  );
}
