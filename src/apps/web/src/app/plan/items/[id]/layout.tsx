import type { Metadata } from "next";

import { ROUTE_METADATA } from "@/lib/site-metadata";

export const metadata: Metadata = ROUTE_METADATA.planItem;

export default function PlanItemLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
