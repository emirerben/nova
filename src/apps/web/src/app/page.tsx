import { getServerSession } from "next-auth";
import { redirect } from "next/navigation";

import KriaEditStory from "@/components/KriaEditStory";
import KriaLifeLanding from "@/components/KriaLifeLanding";
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

  const useScrollComparison = searchParams?.mode === "scroll";

  return (
    <main className="min-h-screen bg-[#ffffff] text-[#30352C]">
      {useScrollComparison ? <KriaEditStory mode="scroll" /> : <KriaLifeLanding />}
    </main>
  );
}
