import type { Metadata, Viewport } from "next";
import "./globals.css";
import Header from "@/components/Header";
import { BRAND_NAME, CANONICAL_WEB_ORIGIN } from "@/lib/brand";
import Providers from "./providers";
import { Toaster } from "@/components/ui/sonner";

export const metadata: Metadata = {
  metadataBase: new URL(CANONICAL_WEB_ORIGIN),
  title: `${BRAND_NAME} — AI video editor for short-form content`,
  description:
    "Turn raw footage into polished short-form videos with Kria, from content planning through the final edit.",
  openGraph: {
    title: `${BRAND_NAME} — AI video editor for short-form content`,
    description:
      "Turn raw footage into polished short-form videos with Kria, from content planning through the final edit.",
    url: CANONICAL_WEB_ORIGIN,
    siteName: BRAND_NAME,
  },
  icons: {
    // The approved standalone DynaPuff wordmark, with a light/dark ink variant.
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      {
        url: "/favicon.svg",
        type: "image/svg+xml",
        media: "(prefers-color-scheme: light)",
      },
      {
        url: "/favicon-white.svg",
        type: "image/svg+xml",
        media: "(prefers-color-scheme: dark)",
      },
    ],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#9BCAFF",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-white text-[#30352C]">
        <Providers>
          <Header />
          {children}
          <Toaster />
        </Providers>
      </body>
    </html>
  );
}
