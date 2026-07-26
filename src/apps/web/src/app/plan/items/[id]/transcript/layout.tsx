import type { Metadata } from "next";

import { ROUTE_METADATA } from "@/lib/site-metadata";

export const metadata: Metadata = ROUTE_METADATA.transcript;

export default function TranscriptLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
