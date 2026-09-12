import { drawMotionFrame } from "./canvaskit.ts";
import { validateMotionInstances, isCompatiblePersistedMotionRuntimeHash } from "./contract.ts";

declare const nativeMotionGlyphs: (text: string) => number[];
declare const nativeMotionWidths: (size: number, glyphs: number[]) => number[];

type Command = unknown[];
class Paint {
  style = "fill";
  width = 1;
  color = [0, 0, 0, 1];
  dash: number[] = [];
  phase = 0;
  setStyle(value: string) { this.style = value; }
  setStrokeCap(_value: unknown) {}
  setStrokeJoin(_value: unknown) {}
  setAntiAlias(_value: boolean) {}
  setStrokeWidth(value: number) { this.width = value; }
  setColor(value: Float32Array) { this.color = Array.from(value); }
  setPathEffect(value: { intervals: number[]; phase: number } | null) {
    this.dash = value?.intervals ?? []; this.phase = value?.phase ?? 0;
  }
  snapshot() { return [this.style, this.width, this.color, this.dash, this.phase]; }
  delete() {}
}
class Path {
  commands: Command[] = [];
  static MakeFromSVGString(value: string) { const path = new Path(); path.commands.push(["svg", value]); return path; }
  moveTo(x: number, y: number) { this.commands.push(["move", x, y]); }
  cubicTo(...values: number[]) { this.commands.push(["cubic", ...values]); }
  close() { this.commands.push(["close"]); }
  delete() {}
}
class Font {
  constructor(readonly size: number) {}
  getGlyphIDs(text: string) { return new Uint16Array(nativeMotionGlyphs(text)); }
  getGlyphWidths(glyphs: Uint16Array) { return new Float32Array(nativeMotionWidths(this.size, Array.from(glyphs))); }
  delete() {}
}
class Image {
  constructor(readonly id: string, readonly dimensions: number[]) {}
  width() { return this.dimensions[0]; }
  height() { return this.dimensions[1]; }
  delete() {}
}
const kit = {
  TRANSPARENT: new Float32Array([0, 0, 0, 0]),
  Paint, Path,
  PaintStyle: { Stroke: "stroke", Fill: "fill" },
  StrokeCap: { Round: "round" }, StrokeJoin: { Round: "round" }, ClipOp: { Intersect: "intersect" },
  parseColorString(value: string) {
    if (!/^#[0-9a-f]{6}$/i.test(value)) throw new Error("Invalid native motion color");
    const n = parseInt(value.slice(1), 16);
    return new Float32Array([(n >> 16) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, 1]);
  },
  multiplyByAlpha(color: Float32Array, alpha: number) { return new Float32Array([color[0], color[1], color[2], color[3] * alpha]); },
  XYWHRect(x: number, y: number, width: number, height: number) { return [x, y, width, height]; },
  RRectXY(rect: number[], rx: number, ry: number) { return [...rect, rx, ry]; },
  PathEffect: { MakeDash(intervals: number[], phase = 0) { return { intervals, phase, delete() {} }; } },
};

/** Uses the production draw functions; only the drawing backend changes. */
export function nativeMotionFrame(instances: unknown, frame: number, width: number, height: number, imageSizes: Record<string, number[]>): Command[] {
  const commands: Command[] = [];
  const canvas = {
    clear(_color: Float32Array) {},
    save() { commands.push(["save"]); return 0; },
    restore() { commands.push(["restore"]); },
    translate(x: number, y: number) { commands.push(["translate", x, y]); },
    scale(x: number, y: number) { commands.push(["scale", x, y]); },
    rotate(degrees: number, px = 0, py = 0) { commands.push(["rotate", degrees, px, py]); },
    skew(x: number, y: number) { commands.push(["skew", x, y]); },
    clipRect(rect: number[]) { commands.push(["clip", rect]); },
    drawPath(path: Path, paint: Paint) { commands.push(["path", path.commands, paint.snapshot()]); },
    drawRect(rect: number[], paint: Paint) { commands.push(["rect", rect, paint.snapshot()]); },
    drawRRect(rect: number[], paint: Paint) { commands.push(["roundRect", rect, paint.snapshot()]); },
    drawCircle(x: number, y: number, radius: number, paint: Paint) { commands.push(["circle", [x, y, radius], paint.snapshot()]); },
    drawText(text: string, x: number, y: number, paint: Paint, font: Font) { commands.push(["text", text, x, y, font.size, paint.snapshot()]); },
    drawImageRect(image: Image, source: number[], dest: number[], paint?: Paint) { commands.push(["image", image.id, source, dest, paint?.snapshot()]); },
  };
  const resources = {
    typeface: { delete() {} },
    images: new Map(Object.entries(imageSizes).map(([id, size]) => [id, new Image(id, size)])),
    font(size: number) { return new Font(size); }, delete() {},
  };
  drawMotionFrame(kit, canvas as never, instances as never, frame, width, height, resources);
  return commands;
}
export { validateMotionInstances, isCompatiblePersistedMotionRuntimeHash };

export function validateNativeMotionInstances(value: unknown, durationFrames: number) {
  const copy = JSON.parse(JSON.stringify(value));
  if (Array.isArray(copy)) for (const instance of copy) {
    const assets = instance?.params?.assets;
    if (!Array.isArray(assets)) continue;
    for (const asset of assets) {
      if (!asset || Object.keys(asset).some(key => key !== "asset_id")) return {ok: false, errors: ["Native images use asset aliases only"]};
      // The draw functions never read this field. Supply an inert value solely
      // for the shared storage-form schema; no storage path enters a recipe.
      asset.gcs_path = "users/native/" + asset.asset_id;
    }
  }
  return validateMotionInstances(copy, durationFrames);
}
