import type { Viewport } from "next";

// The render-status flow is the one user-facing dark-theater surface
// (DESIGN.md §3). Override the global light themeColor so mobile browser
// chrome matches the black canvas instead of tinting it cream.
export const viewport: Viewport = {
  themeColor: "#0c0c0e",
};

export default function TemplateJobsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
