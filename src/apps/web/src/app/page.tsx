import { getServerSession } from "next-auth";
import { redirect } from "next/navigation";

import KriaEditStory from "@/components/KriaEditStory";
import { authOptions } from "@/lib/auth";

export const dynamic = "force-dynamic";

type HomePageProps = {
  searchParams?: {
    mode?: string | string[];
  };
};

export default async function HomePage({ searchParams }: HomePageProps) {
  const session = await getServerSession(authOptions);
  if (session) redirect("/plan");

  const storyMode = searchParams?.mode === "scroll" ? "scroll" : "auto";

  return (
    <main className="min-h-screen bg-[#ffffff] text-[#0c0c0e]">
      <KriaEditStory mode={storyMode} />
    </main>
  );
}
