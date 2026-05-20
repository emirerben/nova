"use client";

import { useRef, useState } from "react";
import type { TemplateListItem } from "@/lib/api";
import TemplateTile from "./TemplateTile";
import TemplatePreviewModal from "./TemplatePreviewModal";

interface Props {
  templates: TemplateListItem[];
}

export default function TemplateGrid({ templates }: Props) {
  const [previewTemplate, setPreviewTemplate] =
    useState<TemplateListItem | null>(null);
  const lastTriggerRef = useRef<HTMLElement | null>(null);

  function openPreview(t: TemplateListItem) {
    if (typeof document !== "undefined") {
      lastTriggerRef.current = document.activeElement as HTMLElement | null;
    }
    setPreviewTemplate(t);
  }

  function closePreview() {
    setPreviewTemplate(null);
  }

  if (templates.length === 0) {
    return (
      <div className="text-center py-20">
        <p className="text-zinc-500">No templates available yet.</p>
      </div>
    );
  }

  return (
    <>
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        {templates.map((t, index) => (
          <TemplateTile
            key={t.id}
            template={t}
            index={index}
            onOpenPreview={openPreview}
          />
        ))}
      </div>

      <TemplatePreviewModal
        template={previewTemplate}
        returnFocusTo={lastTriggerRef.current}
        onClose={closePreview}
      />
    </>
  );
}
