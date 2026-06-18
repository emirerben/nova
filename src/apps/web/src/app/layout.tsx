import type { Metadata } from "next";
import "./globals.css";
import Header from "@/components/Header";
import Providers from "./providers";

export const metadata: Metadata = {
  title: "Nova — Your AI content agent",
  description:
    "An AI agent that gives you video ideas, tells you what to film, and edits every video. You just press record.",
  openGraph: {
    title: "Nova — Your AI content agent",
    description: "An AI agent for your content career.",
  },
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
