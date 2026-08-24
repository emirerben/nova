"use client";

import { MediaRadioCard, STYLE_TILES, TYPE_COPY, TYPE_MEDIA } from "@/app/plan/items/[id]/components/SetupPicker";
import { InkButton } from "@/components/ui/InkButton";
import { Card, CardContent, CardFooter } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function NewVideoFlowFixture() {
  return (
    <main className="mx-auto flex min-h-[100dvh] max-w-[900px] flex-col bg-[#ffffff] px-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] pt-6 text-[#0c0c0e]">
      <style>{`html, body { background: #ffffff !important; } header { display: none; }`}</style>
      <section aria-labelledby="kind-title">
        <p className="text-[12px] text-[#71717a]">Step 1 of 3</p>
        <h1 id="kind-title" className="font-display mt-6 text-[30px] font-medium leading-tight">
          What do you want to make?
        </h1>
        <p className="mt-1.5 text-sm text-[#71717a]">Choose a format. You can change it later.</p>
        <div
          className="scrollbar-none -mx-6 mt-6 flex snap-x snap-mandatory gap-3.5 overflow-x-auto px-6 py-1 [scroll-padding-inline:1.5rem]"
          role="radiogroup"
          aria-label="What kind of video"
        >
          {(["montage", "narrated_planned"] as const).map((value) => (
            <MediaRadioCard
              key={value}
              active={value === "montage"}
              saving={false}
              poster={TYPE_MEDIA[value].poster}
              video={TYPE_MEDIA[value].video}
              scrim="h-3/5"
              label={TYPE_COPY[value].label}
              desc={TYPE_COPY[value].desc}
              meta={TYPE_COPY[value].meta}
              onSelect={() => undefined}
            />
          ))}
        </div>
      </section>

      <section className="mt-10" aria-labelledby="style-title">
        <p className="text-[12px] text-[#71717a]">Step 2 of 3</p>
        <h2 id="style-title" className="font-display mt-6 text-[30px] font-medium leading-tight">
          Choose a visual style
        </h2>
        <p className="mt-1.5 text-sm text-[#71717a]">Choose how your footage should be arranged.</p>
        <div
          className="scrollbar-none -mx-6 mt-6 flex snap-x snap-mandatory gap-3.5 overflow-x-auto px-6 py-1 [scroll-padding-inline:1.5rem] sm:mx-0 sm:grid sm:grid-cols-3 sm:overflow-visible sm:px-0 sm:pb-4 sm:pt-0"
          role="radiogroup"
          aria-label="Montage style"
        >
          {STYLE_TILES.map((tile) => (
            <MediaRadioCard
              key={tile.value}
              active={tile.value === "classic"}
              saving={false}
              poster={tile.poster}
              video={tile.video}
              scrim="h-1/2"
              label={tile.label}
              desc={tile.desc}
              onSelect={() => undefined}
            />
          ))}
        </div>
      </section>

      <section className="mt-8" aria-labelledby="setup-title">
        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
          Music montage · Classic
        </p>
        <h2 id="setup-title" className="font-display mt-1 text-3xl text-[#0c0c0e]">
          Add your clips.
        </h2>
        <Card className="mt-4">
          <Tabs defaultValue="clips">
            <CardContent className="space-y-6 pt-6">
              <TabsList>
                <TabsTrigger value="clips">Clips</TabsTrigger>
                <TabsTrigger value="visuals">Visuals</TabsTrigger>
              </TabsList>
              <div className="flex min-h-[120px] items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white px-6 text-center text-sm text-[#71717a]">
                Drop clips here or choose from your phone.
              </div>
              <Separator />
              <p className="text-sm text-[#3f3f46]">Tell Kria what to keep, avoid, or emphasize.</p>
            </CardContent>
          </Tabs>
          <CardFooter className="hidden items-center justify-end gap-4 border-t pt-6 sm:flex">
            <button className="rounded-md bg-[#0c0c0e] px-4 py-2 text-sm font-medium text-white">
              Create video
            </button>
          </CardFooter>
        </Card>
        <div className="sticky bottom-0 z-20 -mx-5 mt-4 border-t border-zinc-200 bg-[#ffffff] px-5 pb-[max(16px,env(safe-area-inset-bottom))] pt-4 sm:hidden md:mx-0 md:px-0">
          <InkButton>Create video</InkButton>
        </div>
      </section>
    </main>
  );
}
