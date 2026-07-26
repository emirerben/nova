import type { Metadata, Viewport } from "next";
import "./globals.css";
import Header from "@/components/Header";
import { CANONICAL_WEB_ORIGIN } from "@/lib/brand";
import Providers from "./providers";

export const metadata: Metadata = {
  metadataBase: new URL(CANONICAL_WEB_ORIGIN),
  icons: {
    // Lime tile for light browser chrome, white tile + lime fan for dark.
    // Browsers without media support on <link rel="icon"> fall back to the last
    // matching entry; the plain lime tile is listed first as the default.
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
  themeColor: "#fafaf8",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-black text-white">
        <Providers>
          <Header />
          {children}
        </Providers>
      </body>
    </html>
  );
}
