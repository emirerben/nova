import type { Metadata } from "next";

import { ROUTE_METADATA } from "@/lib/site-metadata";

export const metadata: Metadata = ROUTE_METADATA.plan;

export default function PlanLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
