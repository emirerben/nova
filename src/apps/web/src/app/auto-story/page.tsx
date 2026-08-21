import { getServerSession } from "next-auth";
import { redirect } from "next/navigation";

import KriaEditStory from "@/components/KriaEditStory";
import { authOptions } from "@/lib/auth";

export const dynamic = "force-dynamic";

export default async function AutoStoryPage() {
  const session = await getServerSession(authOptions);
  if (session) redirect("/plan");

  return (
    <main className="min-h-screen bg-[#ffffff] text-[#0c0c0e]">
      <KriaEditStory mode="auto" />
    </main>
  );
}
