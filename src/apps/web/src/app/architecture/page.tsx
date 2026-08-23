import type { Metadata } from "next";
import { ArchitectureMap } from "@/components/architecture/ArchitectureMap";

export const metadata: Metadata = {
  title: "System architecture — Kria",
  description: "Explore how Kria moves footage from upload to finished video.",
};

export default function ArchitecturePage() {
  return (
    <div className="h-screen w-screen bg-gray-950">
      <ArchitectureMap />
    </div>
  );
}
