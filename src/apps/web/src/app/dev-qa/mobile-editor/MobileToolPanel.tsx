"use client";

import {
  AlignCenter,
  Captions,
  Clock3,
  Images,
  Layers3,
  Music2,
  Palette,
  SlidersHorizontal,
  Sparkles,
  Type,
  Upload,
  WandSparkles,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type { DockTool } from "@/app/plan/items/[id]/_editor/ToolDock";
import { FontSelect, HexInput } from "@/app/plan/items/[id]/_editor/inspector-fields";
import {
  EDITOR_TEXT_SIZE_MAX,
  EDITOR_TEXT_SIZE_MIN,
  EDITOR_TEXT_SIZE_OPTIONS,
} from "@/app/plan/items/[id]/_editor/text-control-options";
import {
  CAPTION_SIZE_MAX,
  CAPTION_SIZE_MIN,
  CAPTION_STROKE_MAX,
} from "@/app/plan/items/[id]/_editor/caption-control-options";
import {
  TEXT_ELEMENT_ANIMATIONS,
  THEME_TRANSITIONS,
} from "@/lib/overlay-constants";
import {
  LETTER_SPACING_MAX_EM,
  LETTER_SPACING_MIN_EM,
  LINE_SPACING_MAX,
  LINE_SPACING_MIN,
  MAX_WIDTH_FRAC_MAX,
  MAX_WIDTH_FRAC_MIN,
} from "@/lib/overlay-layout";
import { TEXT_PRESETS as PRODUCTION_TEXT_PRESETS } from "@/lib/text-presets";
import TextMotionControls from "@/components/text-motion/TextMotionControls";
import {
  textMotionHasControls,
  type TextMotionConfigV2,
} from "@/lib/text-motion-v2";

export interface MobileToolPanelState {
  text: {
    font: string;
    color: string;
    size: number;
    alignment: string;
    boxPosition: string;
    effect: string;
    motion: TextMotionConfigV2 | null;
    themeTransition: string;
    themeTargetGlyph: string;
    preset: string;
    highlightColor: string;
    strokeWidth: number;
    textCase: string;
    letterSpacing: number;
    lineSpacing: number;
    maxWidthFrac: number;
    shadowEnabled: boolean;
    shadowStyle: string;
    behindSubject: boolean;
  };
  captions: {
    text: string;
    enabled: boolean;
    font: string;
    color: string;
    size: number;
    stroke: number;
    shadow: boolean;
    language: string;
  };
  musicTrack: string;
  musicGain: number;
  visuals: string[];
  overlay: { name: string; durationS: number; position: string } | null;
  look: string;
  clipLook: string;
  transition: string;
  kriaStatus: string;
}

export type MobileToolActionValue =
  | string
  | number
  | boolean
  | null
  | {
      name: string;
      previewUrl: string;
      mediaKind: "image" | "video";
    }
  | Partial<TextMotionConfigV2>;

interface MobileToolPanelProps {
  tool: DockTool;
  state: MobileToolPanelState;
  onAction: (action: string, value?: MobileToolActionValue) => void;
  onClose: () => void;
  onDisabledTap: (reason: string) => void;
}

const LOOKS = ["Clean", "Warm", "Film"];
const MUSIC_TRACKS = ["City Lights", "Golden Hour", "Midnight Ferry"];
const SFX = ["Camera click", "Whoosh", "Soft impact"];
const TEXT_MOTION_V2_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";
const TEXT_BEHIND_SUBJECT_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_BEHIND_SUBJECT_ENABLED === "true";

function ChoiceRow({
  label,
  options,
  selected,
  onSelect,
}: {
  label: string;
  options: string[];
  selected?: string | null;
  onSelect: (value: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <p className="text-[11px] font-medium text-muted-foreground">{label}</p>
      <div className="flex min-w-max gap-2">
        {options.map((option) => (
          <Button
            key={option}
            type="button"
            variant={selected === option ? "secondary" : "outline"}
            className="min-h-11"
            aria-pressed={selected === option}
            onClick={() => onSelect(option)}
          >
            {option[0].toUpperCase() + option.slice(1)}
          </Button>
        ))}
      </div>
    </div>
  );
}

function PanelTabs({
  defaultValue,
  tabs,
  children,
}: {
  defaultValue: string;
  tabs: Array<{ value: string; label: string; icon: React.ReactNode }>;
  children: React.ReactNode;
}) {
  return (
    <Tabs defaultValue={defaultValue} className="min-w-0">
      <TabsList className="scrollbar-none flex h-11 w-full justify-start gap-1 overflow-x-auto rounded-none border-b border-border bg-background px-2 py-0">
        {tabs.map((tab) => (
          <TabsTrigger
            key={tab.value}
            value={tab.value}
            className="min-h-10 flex-none gap-1.5 rounded-md border-b-0 px-3 pb-0 text-xs"
          >
            {tab.icon}
            {tab.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {children}
    </Tabs>
  );
}

const contentClass =
  "scrollbar-none m-0 flex min-h-[104px] items-start gap-3 overflow-x-auto px-3 py-2 data-[state=inactive]:hidden";

export function MobileToolPanel({
  tool,
  state,
  onAction,
  onClose,
  onDisabledTap,
}: MobileToolPanelProps) {
  const title = tool === "nova" ? "Kria" : tool[0].toUpperCase() + tool.slice(1);

  return (
    <section
      data-testid="mobile-tool-panel"
      data-tool={tool}
      aria-label={`${title} controls`}
      className="border-t border-border bg-background"
    >
      <div className="flex h-10 items-center justify-between px-3">
        <p className="text-xs font-semibold">{title}</p>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`Close ${title} controls`}
          className="size-10"
          onClick={onClose}
        >
          <X className="size-4" aria-hidden="true" />
        </Button>
      </div>

      {tool === "text" && (
        <PanelTabs
          defaultValue="style"
          tabs={[
            { value: "style", label: "Style", icon: <Palette className="size-4" /> },
            { value: "layout", label: "Layout", icon: <AlignCenter className="size-4" /> },
            { value: "motion", label: "Motion", icon: <WandSparkles className="size-4" /> },
            { value: "advanced", label: "Advanced", icon: <SlidersHorizontal className="size-4" /> },
            { value: "timing", label: "Timing", icon: <Clock3 className="size-4" /> },
          ]}
        >
          <TabsContent value="style" className={contentClass}>
            <div className="min-w-[220px] space-y-1.5">
              <p className="text-[11px] font-medium text-muted-foreground">Font</p>
              <FontSelect
                value={state.text.font}
                onChange={(value) => onAction("text.font", value)}
                ariaLabelPrefix="Text font"
                triggerClassName="h-11 text-base"
              />
            </div>
            <div className="min-w-[230px] space-y-1.5">
              <div className="flex justify-between text-[11px] text-muted-foreground">
                <span>Size</span><span>{state.text.size}</span>
              </div>
              <div className="flex items-center gap-2">
                <Select
                  value={EDITOR_TEXT_SIZE_OPTIONS.includes(state.text.size) ? String(state.text.size) : "custom"}
                  onValueChange={(value) => {
                    const size = Number(value);
                    if (Number.isFinite(size)) onAction("text.size", size);
                  }}
                >
                  <SelectTrigger aria-label="Font size" className="h-11 w-[76px] text-base">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {!EDITOR_TEXT_SIZE_OPTIONS.includes(state.text.size) && (
                      <SelectItem value="custom">{state.text.size}</SelectItem>
                    )}
                    {EDITOR_TEXT_SIZE_OPTIONS.map((size) => (
                      <SelectItem key={size} value={String(size)}>{size}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Slider
                  aria-label="Font size (fine)"
                  min={EDITOR_TEXT_SIZE_MIN}
                  max={EDITOR_TEXT_SIZE_MAX}
                  step={1}
                  value={[state.text.size]}
                  onValueChange={([value]) => onAction("text.size", value)}
                  className="h-11 min-w-0 flex-1"
                />
              </div>
            </div>
            <div className="min-w-[156px] space-y-1.5">
              <p className="text-[11px] font-medium text-muted-foreground">Fill</p>
              <div className="flex h-11 items-center gap-2">
                <Input
                  type="color"
                  aria-label="Fill color"
                  value={state.text.color}
                  onChange={(event) => onAction("text.color", event.currentTarget.value)}
                  className="h-10 w-12 p-1"
                />
                <HexInput
                  value={state.text.color}
                  onChange={(value) => onAction("text.color", value)}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <p className="text-[11px] font-medium text-muted-foreground">Presets</p>
              <div className="flex min-w-max gap-2">
                {PRODUCTION_TEXT_PRESETS.map((preset) => (
                  <Button
                    key={preset.id}
                    type="button"
                    variant={state.text.preset === preset.id ? "secondary" : "outline"}
                    aria-pressed={state.text.preset === preset.id}
                    className="min-h-11"
                    onClick={() => onAction("text.preset", preset.id)}
                  >
                    {preset.label}
                  </Button>
                ))}
              </div>
            </div>
          </TabsContent>
          <TabsContent value="layout" className={contentClass}>
            <ChoiceRow
              label="Text alignment"
              options={["left", "center", "right"]}
              selected={state.text.alignment}
              onSelect={(value) => onAction("text.alignment", value)}
            />
            <ChoiceRow
              label="Box position"
              options={["left", "center", "right"]}
              selected={state.text.boxPosition}
              onSelect={(value) => onAction("text.boxPosition", value)}
            />
            <div className="min-w-[180px] space-y-1.5">
              <div className="flex justify-between text-[11px] text-muted-foreground">
                <span>Width</span><span>{Math.round(state.text.maxWidthFrac * 100)}%</span>
              </div>
              <Slider
                aria-label="Text width"
                min={MAX_WIDTH_FRAC_MIN * 100}
                max={MAX_WIDTH_FRAC_MAX * 100}
                step={1}
                value={[Math.round(state.text.maxWidthFrac * 100)]}
                onValueChange={([value]) => onAction("text.maxWidthFrac", value / 100)}
                className="h-11"
              />
            </div>
            <div className="space-y-1.5">
              <p className="text-[11px] font-medium text-muted-foreground">Placement</p>
              <Button className="min-h-11" variant="outline" onClick={() => onAction("text.smartPlace")}>
                <Sparkles className="size-4" /> Smart place
              </Button>
            </div>
          </TabsContent>
          <TabsContent value="motion" className={contentClass}>
            <div className="min-w-[220px] space-y-1.5">
              <p className="text-[11px] font-medium text-muted-foreground">Animation</p>
              <Select value={state.text.effect} onValueChange={(value) => onAction("text.effect", value)}>
                <SelectTrigger aria-label="Animation" className="h-11 text-base"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TEXT_ELEMENT_ANIMATIONS.filter(
                    (animation) => animation.value !== "smooth-type" || TEXT_MOTION_V2_UI_ENABLED,
                  ).map((animation) => (
                    <SelectItem key={animation.value} value={animation.value}>{animation.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {TEXT_MOTION_V2_UI_ENABLED &&
              state.text.motion?.version === 2 &&
              textMotionHasControls(state.text.effect) && (
                <div className="min-w-[280px] rounded-md border px-3 pb-3">
                  <TextMotionControls
                    compact
                    effect={state.text.effect}
                    motion={state.text.motion}
                    onChange={(patch) => onAction("text.motion", patch)}
                    onResetLegacy={() => onAction("text.resetMotion")}
                  />
                </div>
              )}
            <div className="min-w-[220px] space-y-1.5">
              <p className="text-[11px] font-medium text-muted-foreground">Theme transition</p>
              <Select value={state.text.themeTransition} onValueChange={(value) => onAction("text.themeTransition", value)}>
                <SelectTrigger aria-label="Theme transition" className="h-11 text-base"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None</SelectItem>
                  {THEME_TRANSITIONS.map((transition) => (
                    <SelectItem key={transition.value} value={transition.value}>{transition.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {state.text.themeTransition === "giant-title-wipe" && (
              <label className="min-w-[150px] text-[11px] font-medium text-muted-foreground">
                Target glyph
                <Input
                  aria-label="Target glyph"
                  maxLength={1}
                  value={state.text.themeTargetGlyph}
                  placeholder="center"
                  onChange={(event) =>
                    onAction("text.themeTargetGlyph", event.currentTarget.value.slice(0, 1))
                  }
                  className="mt-1 h-11 text-base"
                />
              </label>
            )}
            {TEXT_BEHIND_SUBJECT_UI_ENABLED && (
              <div className="flex min-h-11 min-w-[170px] items-center justify-between gap-3 rounded-md border px-3">
                <span className="text-sm">Behind subject</span>
                <Switch
                  aria-label="Behind subject"
                  checked={state.text.behindSubject}
                  onCheckedChange={(checked) => onAction("text.behindSubject", checked)}
                />
              </div>
            )}
          </TabsContent>
          <TabsContent value="advanced" className={contentClass}>
            <div className="min-w-[150px] space-y-1.5">
              <p className="text-[11px] text-muted-foreground">Aa case</p>
              <Select value={state.text.textCase} onValueChange={(value) => onAction("text.textCase", value)}>
                <SelectTrigger aria-label="Text case" className="h-11 text-base"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None</SelectItem>
                  <SelectItem value="upper">Upper</SelectItem>
                  <SelectItem value="lower">Lower</SelectItem>
                  <SelectItem value="title">Title</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <label className="min-w-[116px] text-[11px] text-muted-foreground">
              Letter spacing
              <Input
                type="number"
                aria-label="Letter spacing"
                min={LETTER_SPACING_MIN_EM}
                max={LETTER_SPACING_MAX_EM}
                step={0.01}
                value={state.text.letterSpacing}
                onChange={(event) => onAction("text.letterSpacing", Number(event.currentTarget.value))}
                className="mt-1 h-11 text-base"
              />
            </label>
            <label className="min-w-[116px] text-[11px] text-muted-foreground">
              Line spacing
              <Input
                type="number"
                aria-label="Line spacing"
                min={LINE_SPACING_MIN}
                max={LINE_SPACING_MAX}
                step={0.05}
                value={state.text.lineSpacing}
                onChange={(event) => onAction("text.lineSpacing", Number(event.currentTarget.value))}
                className="mt-1 h-11 text-base"
              />
            </label>
            <div className="min-w-[156px] space-y-1.5">
              <p className="text-[11px] text-muted-foreground">Highlight</p>
              <div className="flex h-11 items-center gap-2">
                <Input type="color" aria-label="Highlight color" value={state.text.highlightColor} onChange={(event) => onAction("text.highlightColor", event.currentTarget.value)} className="h-10 w-12 p-1" />
                <HexInput value={state.text.highlightColor} onChange={(value) => onAction("text.highlightColor", value)} ariaLabel="Highlight color hex" />
              </div>
            </div>
            <div className="min-w-[170px] space-y-1.5">
              <p className="text-[11px] text-muted-foreground">Shadow</p>
              <Select value={state.text.shadowEnabled ? state.text.shadowStyle : "off"} onValueChange={(value) => onAction("text.shadow", value)}>
                <SelectTrigger aria-label="Shadow effect" className="h-11 text-base"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="standard">Standard</SelectItem>
                  <SelectItem value="high_visibility">High visibility</SelectItem>
                  <SelectItem value="off">Off</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="min-w-[170px] space-y-1.5">
              <div className="flex justify-between text-[11px] text-muted-foreground"><span>Stroke</span><span>{state.text.strokeWidth}</span></div>
              <Slider aria-label="Stroke width" min={0} max={12} step={1} value={[state.text.strokeWidth]} onValueChange={([value]) => onAction("text.strokeWidth", value)} className="h-11" />
            </div>
          </TabsContent>
          <TabsContent value="timing" className={contentClass}>
            <Button variant="outline" className="min-h-11" onClick={() => onAction("text.startHere")}>Start here</Button>
            <Button variant="outline" className="min-h-11" onClick={() => onAction("text.endHere")}>End here</Button>
            <Button variant="destructive" className="min-h-11" onClick={() => onAction("text.delete")}>Delete text</Button>
          </TabsContent>
        </PanelTabs>
      )}

      {tool === "captions" && (
        <PanelTabs
          defaultValue="edit"
          tabs={[
            { value: "edit", label: "Edit", icon: <Captions className="size-4" /> },
            { value: "style", label: "Style", icon: <Palette className="size-4" /> },
            { value: "language", label: "Language", icon: <Type className="size-4" /> },
          ]}
        >
          <TabsContent value="edit" className={contentClass}>
            <Textarea
              aria-label="Caption cue text"
              value={state.captions.text}
              onChange={(event) => onAction("captions.text", event.currentTarget.value)}
              className="min-h-[88px] min-w-[250px] resize-none text-base"
            />
            <div className="flex min-h-11 min-w-[132px] items-center justify-between gap-3 rounded-md border px-3">
              <span className="text-sm">Captions</span>
              <Switch
                aria-label="Captions enabled"
                checked={state.captions.enabled}
                onCheckedChange={(checked) => onAction("captions.enabled", checked)}
              />
            </div>
            <Button
              variant="outline"
              aria-disabled="true"
              className="min-h-11 min-w-max"
              onClick={() => onDisabledTap("This is the first caption, so there is no previous cue to merge")}
            >
              Merge previous
            </Button>
          </TabsContent>
          <TabsContent value="style" className={contentClass}>
            <div className="space-y-1.5">
              <p className="text-[11px] text-muted-foreground">Font</p>
              <FontSelect
                value={state.captions.font}
                onChange={(value) => onAction("captions.font", value)}
                ariaLabelPrefix="Caption font"
                triggerClassName="h-11 min-w-[220px] text-base"
              />
            </div>
            <Input
              type="color"
              aria-label="Caption color"
              value={state.captions.color}
              onChange={(event) => onAction("captions.color", event.currentTarget.value)}
              className="mt-5 h-11 w-20 p-1"
            />
            <div className="min-w-[150px]">
              <div className="flex justify-between text-[11px] text-muted-foreground"><span>Size</span><span>{state.captions.size}</span></div>
              <Slider aria-label="Caption size" min={CAPTION_SIZE_MIN} max={CAPTION_SIZE_MAX} value={[state.captions.size]} onValueChange={([value]) => onAction("captions.size", value)} className="h-11" />
            </div>
            <div className="min-w-[150px]">
              <div className="flex justify-between text-[11px] text-muted-foreground"><span>Stroke</span><span>{state.captions.stroke}</span></div>
              <Slider aria-label="Caption stroke" min={0} max={CAPTION_STROKE_MAX} value={[state.captions.stroke]} onValueChange={([value]) => onAction("captions.stroke", value)} className="h-11" />
            </div>
            <div className="flex min-h-11 min-w-[120px] items-center justify-between gap-3 rounded-md border px-3">
              <span className="text-sm">Shadow</span>
              <Switch aria-label="Caption shadow" checked={state.captions.shadow} onCheckedChange={(checked) => onAction("captions.shadow", checked)} />
            </div>
          </TabsContent>
          <TabsContent value="language" className={contentClass}>
            <ChoiceRow label="Transcription language" options={["English", "Turkish"]} selected={state.captions.language} onSelect={(value) => onAction("captions.language", value)} />
            <Button className="mt-5 min-h-11 min-w-max" onClick={() => onAction("captions.retranscribe")}>Re-transcribe</Button>
          </TabsContent>
        </PanelTabs>
      )}

      {tool === "visuals" && (
        <PanelTabs
          defaultValue="add"
          tabs={[
            { value: "add", label: "Add", icon: <Images className="size-4" /> },
            { value: "blocks", label: "Blocks", icon: <Layers3 className="size-4" /> },
          ]}
        >
          <TabsContent value="add" className={contentClass}>
            <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-4 text-sm font-medium">
              <Upload className="size-4" /> Upload
              <Input
                type="file"
                accept="image/*,video/*"
                aria-label="Upload visual"
                className="sr-only"
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  if (!file) return;
                  onAction("visuals.upload", {
                    name: file.name,
                    previewUrl: URL.createObjectURL(file),
                    mediaKind: file.type.startsWith("video/") ? "video" : "image",
                  });
                }}
              />
            </label>
            {[
              ["visuals.montage", "Montage"],
              ["visuals.media", "Media block"],
              ["visuals.sequence", "Sequence"],
              ["visuals.textCard", "Text card"],
            ].map(([action, label]) => (
              <Button key={action} variant="outline" className="min-h-11 min-w-max" onClick={() => onAction(action)}>{label}</Button>
            ))}
          </TabsContent>
          <TabsContent value="blocks" className={contentClass}>
            {state.visuals.length === 0 ? (
              <p className="py-3 text-sm text-muted-foreground">Add a visual block to edit its timing and display mode.</p>
            ) : (
              <>
                <ChoiceRow label="Display mode" options={["Fullscreen", "Overlay"]} onSelect={(value) => onAction("visuals.display", value)} />
                <Button variant="outline" className="mt-5 min-h-11" onClick={() => onAction("visuals.retime")}>Retime</Button>
                <Button variant="destructive" className="mt-5 min-h-11" onClick={() => onAction("visuals.delete")}>Delete visual</Button>
              </>
            )}
          </TabsContent>
        </PanelTabs>
      )}

      {tool === "sounds" && (
        <PanelTabs
          defaultValue="sfx"
          tabs={[
            { value: "sfx", label: "SFX", icon: <Sparkles className="size-4" /> },
            { value: "music", label: "Music", icon: <Music2 className="size-4" /> },
            { value: "mix", label: "Mix", icon: <SlidersHorizontal className="size-4" /> },
          ]}
        >
          <TabsContent value="sfx" className={contentClass}>
            <ChoiceRow label="Add at the playhead" options={SFX} onSelect={(value) => onAction("sounds.sfx", value)} />
          </TabsContent>
          <TabsContent value="music" className={contentClass}>
            <ChoiceRow label="Soundtrack" options={MUSIC_TRACKS} selected={state.musicTrack} onSelect={(value) => onAction("sounds.music", value)} />
            <Button variant="destructive" className="mt-5 min-h-11 min-w-max" onClick={() => onAction("sounds.removeMusic")}>Remove music</Button>
          </TabsContent>
          <TabsContent value="mix" className={contentClass}>
            <div className="min-w-[240px]">
              <div className="flex justify-between text-xs text-muted-foreground"><span>Music level</span><span>{state.musicGain}%</span></div>
              <Slider aria-label="Music level" min={0} max={100} value={[state.musicGain]} onValueChange={([value]) => onAction("sounds.gain", value)} className="h-11" />
            </div>
          </TabsContent>
        </PanelTabs>
      )}

      {tool === "overlays" && (
        <PanelTabs
          defaultValue="add"
          tabs={[
            { value: "add", label: "Add", icon: <Upload className="size-4" /> },
            { value: "place", label: "Place", icon: <Layers3 className="size-4" /> },
          ]}
        >
          <TabsContent value="add" className={contentClass}>
            <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-4 text-sm font-medium">
              <Upload className="size-4" /> Upload overlay
              <Input type="file" accept="image/*,video/*" aria-label="Upload overlay" className="sr-only" onChange={(event) => onAction("overlays.upload", event.currentTarget.files?.[0]?.name ?? "Uploaded overlay")} />
            </label>
            <Button className="min-h-11 min-w-max" onClick={() => onAction("overlays.suggest")}><Sparkles className="size-4" /> Suggest overlay</Button>
          </TabsContent>
          <TabsContent value="place" className={contentClass}>
            {state.overlay ? (
              <>
                <ChoiceRow label="Position" options={["Left", "Center", "Right"]} selected={state.overlay.position} onSelect={(value) => onAction("overlays.position", value)} />
                <div className="min-w-[170px]">
                  <div className="flex justify-between text-xs text-muted-foreground"><span>Duration</span><span>{state.overlay.durationS.toFixed(1)}s</span></div>
                  <Slider aria-label="Overlay duration" min={0.5} max={6} step={0.1} value={[state.overlay.durationS]} onValueChange={([value]) => onAction("overlays.duration", value)} className="h-11" />
                </div>
                <Button variant="destructive" className="mt-5 min-h-11" onClick={() => onAction("overlays.delete")}>Delete overlay</Button>
              </>
            ) : (
              <p className="py-3 text-sm text-muted-foreground">Upload or suggest an overlay before placing it.</p>
            )}
          </TabsContent>
        </PanelTabs>
      )}

      {tool === "styles" && (
        <PanelTabs
          defaultValue="edit"
          tabs={[
            { value: "edit", label: "Edit Look", icon: <Palette className="size-4" /> },
            { value: "clip", label: "Clip", icon: <Images className="size-4" /> },
            { value: "transition", label: "Transition", icon: <WandSparkles className="size-4" /> },
          ]}
        >
          <TabsContent value="edit" className={contentClass}><ChoiceRow label="Whole edit" options={LOOKS} selected={state.look} onSelect={(value) => onAction("styles.look", value)} /></TabsContent>
          <TabsContent value="clip" className={contentClass}><ChoiceRow label="Selected clip" options={LOOKS} selected={state.clipLook} onSelect={(value) => onAction("styles.clipLook", value)} /></TabsContent>
          <TabsContent value="transition" className={contentClass}><ChoiceRow label="After selected clip" options={["Cut", "Dissolve", "Dip"]} selected={state.transition} onSelect={(value) => onAction("styles.transition", value)} /></TabsContent>
        </PanelTabs>
      )}

      {tool === "nova" && (
        <div className="flex min-h-[104px] items-start gap-3 overflow-x-auto px-3 py-2">
          <div className="min-w-[220px] rounded-md border bg-muted/30 p-3">
            <p className="text-sm font-medium">Tighten the opening</p>
            <p className="mt-1 text-xs text-muted-foreground">Remove 0.2s from the first clip.</p>
            <p className="mt-2 text-xs font-medium">{state.kriaStatus}</p>
          </div>
          <Button className="min-h-11" onClick={() => onAction("nova.accept")}>Accept</Button>
          <Button variant="outline" className="min-h-11" onClick={() => onAction("nova.reject")}>Reject</Button>
        </div>
      )}
    </section>
  );
}
