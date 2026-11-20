import KriaEditStory from "@/components/KriaEditStory";
import KriaLifeLanding from "@/components/KriaLifeLanding";

export const dynamic = "force-dynamic";

type HomePageProps = {
  searchParams?: {
    mode?: string | string[];
  };
};

export default async function HomePage({ searchParams }: HomePageProps) {
  const useScrollComparison = searchParams?.mode === "scroll";

  return (
    <main className="min-h-screen bg-[#ffffff] text-[#30352C]">
      {useScrollComparison ? <KriaEditStory mode="scroll" /> : <KriaLifeLanding />}
    </main>
  );
}
