import type { Metadata } from "next";

import { ROUTE_METADATA } from "@/lib/site-metadata";

export const metadata: Metadata = ROUTE_METADATA.generative;

export default function GenerativeLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
