import type { FrameState } from "@/lib/carousel-preview";
import { CANVAS_H, CANVAS_W, DEFAULT_GEOMETRY, transformFor } from "@/lib/carousel-preview";
import {
  SHADOW_DY_PX,
  SHADOW_SIGMA_PX,
  FOCUSED_Z_INDEX,
  cardStyleFor,
  composeTransform,
} from "@/app/plan/items/[id]/_editor/carousel-preview-impl/card-style";

function frame(overrides: Partial<FrameState> & { tS: number; scrollX: number }): FrameState {
  return { focusCard: null, focusT: 0, dim: 0, ...overrides };
}

describe("composeTransform", () => {
  it("cover_flow-style order (rotateBeforeTranslate=false): rotateY() translateZ() scale()", () => {
    expect(composeTransform(false, 0, 0, 12.5, -40, 0.9, 0.9)).toBe(
      "rotateY(12.5deg) translateZ(-40px) scale(0.9, 0.9)",
    );
  });

  it("flipbook-style order (rotateBeforeTranslate=true): translateZ() rotateY()", () => {
    expect(composeTransform(true, 0, 0, -18, -75, 1, 1)).toBe(
      "translateZ(-75px) rotateY(-18deg) scale(1, 1)",
    );
  });

  it("omits the leading translate() entirely when dx/dy are both 0", () => {
    const s = composeTransform(false, 0, 0, 0, 0, 1, 1);
    expect(s.startsWith("translate(")).toBe(false);
  });

  it("prepends translate(dx, dy) OUTSIDE the effect-ordered functions when non-zero", () => {
    expect(composeTransform(false, 10, -20, 0, 0, 2, 2.5)).toBe(
      "translate(10px, -20px) rotateY(0deg) translateZ(0px) scale(2, 2.5)",
    );
    expect(composeTransform(true, 10, -20, 0, -50, 1, 1)).toBe(
      "translate(10px, -20px) translateZ(-50px) rotateY(0deg) scale(1, 1)",
    );
  });

  it("rounds values to 4 decimal places", () => {
    expect(composeTransform(false, 0, 0, 1 / 3, 0, 1, 1)).toBe("rotateY(0.3333deg) translateZ(0px) scale(1, 1)");
  });
});

describe("cardStyleFor — non-focused cards", () => {
  const geo = DEFAULT_GEOMETRY;

  it("scale_sweep: transform matches transformFor's own values, no shadow at t=0 edge, box position from CardTransform.x/y", () => {
    const fstate = frame({ tS: 0, scrollX: 0 });
    const style = cardStyleFor("scale_sweep", 0, fstate, 0, 0, geo, CANVAS_W);
    const t = transformFor("scale_sweep", 0, 0, geo, CANVAS_W, { positionScrollX: 0 });

    expect(style.left).toBe(t.x);
    expect(style.top).toBe(t.y);
    expect(style.width).toBe(geo.cardW);
    expect(style.height).toBe(geo.cardH);
    expect(style.transform).toBe(
      `rotateY(0deg) translateZ(0px) scale(${t.scale}, ${t.scale})`,
    );
    expect(style.zIndex).toBe(t.zIndex);
    expect(style.opacity).toBe(t.opacity);
    expect(style.borderRadius).toBe(geo.cornerRadius);
    expect(style.isFocused).toBe(false);
    expect(style.dim).toBe(0);
  });

  it("cover_flow: a rotated/receded card gets a boxShadow proportional to shadowAlpha", () => {
    const fstate = frame({ tS: 0, scrollX: 900 }); // off-center -> nonzero rotate/shadow
    const style = cardStyleFor("cover_flow", 0, fstate, 900, 900, geo, CANVAS_W);
    const t = transformFor("cover_flow", 900, 0, geo, CANVAS_W, { positionScrollX: 900 });

    expect(t.shadowAlpha).toBeGreaterThan(0);
    expect(style.boxShadow).toBe(
      `0 ${SHADOW_DY_PX}px ${SHADOW_SIGMA_PX}px rgba(0, 0, 0, ${Math.round(t.shadowAlpha * 10000) / 10000})`,
    );
  });

  it("flipbook: rotateBeforeTranslate=true produces translateZ() before rotateY()", () => {
    const fstate = frame({ tS: 0, scrollX: 900 });
    const style = cardStyleFor("flipbook", 0, fstate, 900, 900, geo, CANVAS_W);
    const translateZIdx = style.transform.indexOf("translateZ(");
    const rotateYIdx = style.transform.indexOf("rotateY(");
    expect(translateZIdx).toBeGreaterThanOrEqual(0);
    expect(rotateYIdx).toBeGreaterThan(translateZIdx);
  });

  it("applies the frame's dim to a card that is NOT the focused one", () => {
    const fstate = frame({ tS: 0, scrollX: 0, focusCard: 1, focusT: 1.0, dim: 0.55 });
    const style = cardStyleFor("scale_sweep", 0, fstate, 0, 0, geo, CANVAS_W);
    expect(style.dim).toBe(0.55);
  });

  it("applies zero dim when no card is focused at all", () => {
    const fstate = frame({ tS: 0, scrollX: 0 });
    const style = cardStyleFor("scale_sweep", 0, fstate, 0, 0, geo, CANVAS_W);
    expect(style.dim).toBe(0);
  });

  it("uses the LAGGED scroll for the effect-progress transform and the frame's OWN scroll for layout position", () => {
    const geoLocal = DEFAULT_GEOMETRY;
    const progressScrollX = 0; // previous frame: card 0 dead-center -> triangle peak
    const positionScrollX = 400; // this frame: scrolled onward
    const fstate = frame({ tS: 0, scrollX: positionScrollX });
    const style = cardStyleFor("scale_sweep", 0, fstate, progressScrollX, positionScrollX, geoLocal, CANVAS_W);

    const expectedPose = transformFor("scale_sweep", progressScrollX, 0, geoLocal, CANVAS_W, {
      positionScrollX,
    });
    expect(style.left).toBe(expectedPose.x); // layout position reflects THIS frame's scroll
    expect(style.transform).toContain(`scale(${expectedPose.scale}, ${expectedPose.scale})`); // pose reflects the LAGGED scroll
  });
});

describe("cardStyleFor — focused card lerp", () => {
  const geo = DEFAULT_GEOMETRY;

  it("focusT === 0 is excluded from the focused branch entirely (continuous limit with the unfocused pose)", () => {
    const unfocused = frame({ tS: 0, scrollX: 0 });
    const focusedAtZero = frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 0 });
    const a = cardStyleFor("cover_flow", 0, unfocused, 0, 0, geo, CANVAS_W);
    const b = cardStyleFor("cover_flow", 0, focusedAtZero, 0, 0, geo, CANVAS_W);
    expect(b.isFocused).toBe(false);
    expect(b).toEqual(a);
  });

  function round4(n: number): number {
    return Math.round(n * 10000) / 10000;
  }

  it("focusT === 1: fills the canvas exactly — centered, scaled to CANVAS_W x CANVAS_H, no rotation/depth/radius/shadow", () => {
    // Card index 1 (not 0) so the card starts OFF-center at scrollX=0 and the
    // recentering translate() is actually non-zero — exercises the
    // translate-prefix path, not just the (dx=dy=0) omission case.
    const fstate = frame({ tS: 0, scrollX: 0, focusCard: 1, focusT: 1.0 });
    const style = cardStyleFor("cover_flow", 1, fstate, 0, 0, geo, CANVAS_W);

    expect(style.isFocused).toBe(true);
    expect(style.borderRadius).toBe(0);
    expect(style.boxShadow).toBe("none");
    expect(style.opacity).toBe(1);
    expect(style.dim).toBe(0);
    expect(style.zIndex).toBe(FOCUSED_Z_INDEX);

    const t = transformFor("cover_flow", 0, 1, geo, CANVAS_W, { positionScrollX: 0 });
    const cx = t.x + geo.cardW / 2;
    const cy = t.y + geo.cardH / 2;
    const expectedDx = CANVAS_W / 2 - cx;
    const expectedDy = CANVAS_H / 2 - cy;
    expect(expectedDx).not.toBe(0); // sanity: this card really is off-center pre-focus
    const expectedScaleX = round4(CANVAS_W / geo.cardW);
    const expectedScaleY = round4(CANVAS_H / geo.cardH);

    expect(style.transform).toBe(
      `translate(${round4(expectedDx)}px, ${round4(expectedDy)}px) ` +
        `rotateY(0deg) translateZ(0px) scale(${expectedScaleX}, ${expectedScaleY})`,
    );
  });

  it("intermediate focusT lerps borderRadius linearly between cornerRadius and 0", () => {
    const fstate = frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 0.25 });
    const style = cardStyleFor("scale_sweep", 0, fstate, 0, 0, geo, CANVAS_W);
    expect(style.borderRadius).toBeCloseTo(geo.cornerRadius * 0.75, 6);
  });

  it("focusT is clamped to [0, 1] even if the FrameState carries an out-of-range value", () => {
    const over = frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 1.5 });
    const style = cardStyleFor("cover_flow", 0, over, 0, 0, geo, CANVAS_W);
    const atOne = cardStyleFor(
      "cover_flow",
      0,
      frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 1.0 }),
      0,
      0,
      geo,
      CANVAS_W,
    );
    expect(style).toEqual(atOne);
  });

  it("only the focused card index enters the focused branch — other cards render their normal pose even mid-focus", () => {
    const fstate = frame({ tS: 0, scrollX: 0, focusCard: 0, focusT: 0.5, dim: 0.3 });
    const other = cardStyleFor("scale_sweep", 1, fstate, 0, 0, geo, CANVAS_W);
    expect(other.isFocused).toBe(false);
    expect(other.dim).toBe(0.3);
  });
});
