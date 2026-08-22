"use client";

/**
 * "Plan with Kria" thread — the guided-edit planning conversation, lifted out
 * of the item setup zone into a Sheet (bottom sheet on phones, right panel on
 * desktop). The setup page stays receipt → title → uploader → Tell Kria →
 * Generate; this panel is the only place the multi-turn conversation lives
 * (DESIGN.md §12). Body is the existing EditProposalCard, unchanged except
 * for `defaultConversationOpen` so it starts on the conversation surface
 * instead of behind a "Plan edit" button morph.
 */

import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import type { PlanItem } from "@/lib/plan-api";
import EditProposalCard from "./EditProposalCard";

export default function PlanThreadPanel({
  open,
  onOpenChange,
  item,
  hasPoolMedia,
  onRefresh,
  onChanged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  item: PlanItem;
  hasPoolMedia?: boolean;
  onRefresh?: () => void;
  onChanged: (item: PlanItem) => void;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="inset-x-0 bottom-0 top-auto flex h-[88dvh] flex-col overflow-y-auto rounded-t-2xl sm:inset-y-0 sm:left-auto sm:right-0 sm:h-full sm:w-[480px] sm:rounded-none"
      >
        <SheetHeader>
          <SheetTitle>Plan with Kria</SheetTitle>
        </SheetHeader>
        <div className="mt-2 flex-1">
          <EditProposalCard
            item={item}
            hasPoolMedia={hasPoolMedia}
            onRefresh={onRefresh}
            onChanged={onChanged}
            defaultConversationOpen
          />
        </div>
      </SheetContent>
    </Sheet>
  );
}
