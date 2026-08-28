import { expect, test, type Locator } from "@playwright/test";
import fontRegistry from "../src/data/font-registry.json";
import {
  expectNoHorizontalOverflow,
  installSyntheticTouchCapture,
  qaData,
  qaNumber,
} from "./mobile-helpers";

type QaWindow = { id: string; inS: number; durationS: number };

const PRODUCTION_FONT_NAMES = Object.entries(fontRegistry.fonts)
  .filter(([, entry]) => !("deprecated" in entry && entry.deprecated === true))
  .map(([name]) => name);

const PRODUCTION_TEXT_ANIMATIONS = [
  "Fade in",
  "Pop in",
  "Scale up",
  "Slide up",
  "Slide down",
  "Bounce",
  "Typewriter",
  "Stream in",
  "Staggered slice",
  "Ink reveal",
  "Handwriting",
  "Dissolve out",
  "Smooth type",
  "None",
];

async function dragOnTarget(target: Locator, dx: number, dy = 0) {
  const box = await target.boundingBox();
  if (!box) throw new Error("Timeline target is not visible");
  const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await target.evaluate(
    (element, point) => {
      for (const [type, x, y] of [
        ["pointerdown", point.x, point.y],
        [
          "pointermove",
          point.x + point.dx * 0.33,
          point.y + point.dy * 0.33,
        ],
        [
          "pointermove",
          point.x + point.dx * 0.66,
          point.y + point.dy * 0.66,
        ],
        ["pointermove", point.x + point.dx, point.y + point.dy],
        ["pointerup", point.x + point.dx, point.y + point.dy],
      ] as const) {
        element.dispatchEvent(
          new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: x,
            clientY: y,
            pointerId: 29,
            pointerType: "touch",
            isPrimary: true,
          }),
        );
      }
    },
    { ...start, dx, dy },
  );
}

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/mobile-editor");
  await installSyntheticTouchCapture(page);
});

test("mobile timeline fits the viewport with fixed playhead, 44px handles, and icon dock", async ({
  page,
}) => {
  await expectNoHorizontalOverflow(page);
  const shellMetrics = await page.locator("main").evaluate((element) => {
    const header = element.querySelector("header")?.getBoundingClientRect();
    return {
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      headerRight: header?.right ?? Number.POSITIVE_INFINITY,
      viewportWidth: window.innerWidth,
    };
  });
  expect(shellMetrics.scrollWidth).toBe(shellMetrics.clientWidth);
  expect(shellMetrics.headerRight).toBeLessThanOrEqual(
    shellMetrics.viewportWidth,
  );
  const viewport = page.getByTestId("pocket-timeline-viewport");
  const playhead = page.getByTestId("pocket-ministrip-playhead");
  const viewportBox = await viewport.boundingBox();
  const playheadBox = await playhead.boundingBox();
  expect(viewportBox).not.toBeNull();
  expect(playheadBox).not.toBeNull();
  expect(Math.abs(playheadBox!.x - (viewportBox!.x + 21))).toBeLessThan(2);

  for (const name of [/Trim clip start/, /Trim clip end/]) {
    const box = await page.getByRole("button", { name }).boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }

  const dockBox = await page.getByTestId("pocket-dock").boundingBox();
  expect(dockBox).not.toBeNull();
  expect(dockBox!.y + dockBox!.height).toBeLessThanOrEqual(
    await page.evaluate(() => window.innerHeight),
  );
  await expect(page.getByRole("button", { name: "Visuals tool" })).toBeVisible();
  await expect(page.getByTestId("pocket-timeline-lane-captions")).toBeAttached();
  await expect(page.getByTestId("pocket-timeline-lane-music")).toBeAttached();
});

test("touch trim records one undo step, ripples later clips, and Undo restores it", async ({
  page,
}) => {
  const before = await qaData<QaWindow[]>(page, "data-windows");
  await dragOnTarget(page.getByRole("button", { name: /Trim clip end/ }), -48);

  const after = await qaData<QaWindow[]>(page, "data-windows");
  expect(after[0].durationS).toBeLessThan(before[0].durationS);
  expect(await qaNumber(page, "data-history-len")).toBe(1);
  await expect(page.getByRole("button", { name: "Undo" })).toBeVisible();

  await page.getByRole("button", { name: "Undo" }).click();
  expect(await qaData<QaWindow[]>(page, "data-windows")).toEqual(before);
});

test("filmstrip dragging scrubs without changing the selected source window", async ({
  page,
}) => {
  const before = await qaData<QaWindow[]>(page, "data-windows");
  const beforeTime = await qaNumber(page, "data-current-time");
  await dragOnTarget(page.getByTestId("pocket-timeline-viewport"), -72);

  expect(await qaData<QaWindow[]>(page, "data-windows")).toEqual(before);
  expect(await qaNumber(page, "data-current-time")).toBeGreaterThan(beforeTime);
});

test("filmstrip samples the exact source window and follows touch every frame", async ({
  page,
}) => {
  const firstStrip = page.getByTestId("editor-filmstrip").first();
  const firstWindow = (await qaData<QaWindow[]>(page, "data-windows"))[0];
  const samples =
    (await firstStrip.getAttribute("data-sample-times"))
      ?.split(",")
      .filter(Boolean)
      .map(Number) ?? [];
  expect(samples.length).toBeGreaterThanOrEqual(6);
  for (const sample of samples) {
    expect(sample).toBeGreaterThanOrEqual(firstWindow.inS);
    expect(sample).toBeLessThanOrEqual(firstWindow.inS + firstWindow.durationS);
  }
  samples.forEach((sample, index) => {
    expect(sample).toBeCloseTo(
      firstWindow.inS + ((index + 0.5) / samples.length) * firstWindow.durationS,
      2,
    );
  });

  const initialRangeKey = await firstStrip.getAttribute("data-source-range-key");
  expect(initialRangeKey).not.toBeNull();
  await expect(firstStrip).toHaveAttribute(
    "data-rendered-range-key",
    initialRangeKey!,
  );
  await page.getByRole("button", { name: "Zoom timeline in" }).click();
  await expect(firstStrip).not.toHaveAttribute(
    "data-source-range-key",
    initialRangeKey!,
  );
  const firstZoomRangeKey = await firstStrip.getAttribute(
    "data-source-range-key",
  );
  await page.getByRole("button", { name: "Zoom timeline in" }).click();
  await expect(firstStrip).not.toHaveAttribute(
    "data-source-range-key",
    firstZoomRangeKey!,
  );
  const nextRangeKey = await firstStrip.getAttribute("data-source-range-key");
  const renderedDuringDecode = await firstStrip.getAttribute(
    "data-rendered-range-key",
  );
  expect([initialRangeKey, firstZoomRangeKey, nextRangeKey]).toContain(
    renderedDuringDecode,
  );
  await expect(firstStrip).toHaveAttribute(
    "data-rendered-range-key",
    nextRangeKey!,
  );

  const viewport = page.getByTestId("pocket-timeline-viewport");
  const scrollSamples = await viewport.evaluate(async (element) => {
    const box = element.getBoundingClientRect();
    const startX = box.left + box.width * 0.7;
    const y = box.top + box.height / 2;
    const dispatch = (type: string, x: number) =>
      element.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          clientX: x,
          clientY: y,
          pointerId: 44,
          pointerType: "touch",
          isPrimary: true,
        }),
      );
    dispatch("pointerdown", startX);
    const positions: number[] = [];
    for (let index = 1; index <= 6; index += 1) {
      dispatch("pointermove", startX - index * 12);
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => resolve()),
      );
      positions.push(element.scrollLeft);
    }
    dispatch("pointerup", startX - 72);
    return positions;
  });
  expect(
    scrollSamples.every(
      (value, index) => index === 0 || value >= scrollSamples[index - 1],
    ),
  ).toBe(true);
  expect(
    new Set(scrollSamples.map((value) => Math.round(value))).size,
  ).toBeGreaterThan(3);
});

test("playback scroll follows decoded frames instead of native timeupdate steps", async ({
  page,
}) => {
  const viewport = page.getByTestId("pocket-timeline-viewport");
  const positions = await viewport.evaluate(async (element) => {
    const video = document.querySelector("video");
    if (!video) throw new Error("Preview video is missing");
    await video.play();
    const samples = await new Promise<number[]>((resolve, reject) => {
      const values: number[] = [];
      const timeout = window.setTimeout(
        () => reject(new Error("Decoded frame clock did not advance")),
        5_000,
      );
      const sample = () => {
        window.setTimeout(() => {
          values.push(element.scrollLeft);
          if (values.length >= 12) {
            window.clearTimeout(timeout);
            resolve(values);
            return;
          }
          video.requestVideoFrameCallback(sample);
        }, 0);
      };
      video.requestVideoFrameCallback(sample);
    });
    video.pause();
    return samples;
  });

  const rounded = positions.map((value) => Math.round(value * 10) / 10);
  expect(new Set(rounded).size).toBeGreaterThan(4);
  expect(
    rounded.every(
      (value, index) => index === 0 || value >= rounded[index - 1],
    ),
  ).toBe(true);
});

test("trim keeps the current filmstrip stable, then seeks the exact new source window", async ({
  page,
}) => {
  const strip = page.getByTestId("editor-filmstrip").first();
  await expect(strip).toHaveAttribute("data-rendered-range-key", /.+/);
  const beforeKey = await strip.getAttribute("data-source-range-key");
  const handle = page.getByRole("button", { name: /Trim clip start/ });
  const box = await handle.boundingBox();
  if (!box) throw new Error("Trim handle is not visible");
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };

  await handle.dispatchEvent("pointerdown", {
    pointerId: 91,
    pointerType: "touch",
    isPrimary: true,
    clientX: point.x,
    clientY: point.y,
  });
  await handle.dispatchEvent("pointermove", {
    pointerId: 91,
    pointerType: "touch",
    isPrimary: true,
    clientX: point.x + 24,
    clientY: point.y,
  });
  await expect(strip).toHaveAttribute("data-source-range-key", beforeKey!);

  await handle.dispatchEvent("pointerup", {
    pointerId: 91,
    pointerType: "touch",
    isPrimary: true,
    clientX: point.x + 24,
    clientY: point.y,
  });
  await expect(strip).not.toHaveAttribute("data-source-range-key", beforeKey!);

  const firstWindow = (await qaData<QaWindow[]>(page, "data-windows"))[0];
  await page.getByRole("button", { name: /Clip 1,/ }).click();
  const outputTime = await qaNumber(page, "data-current-time");
  await expect
    .poll(() => page.locator("video").evaluate((video) => video.currentTime))
    .toBeCloseTo(firstWindow.inS + outputTime, 1);
});

test("transport, zoom, clip actions, and boundary reasons all respond", async ({ page }) => {
  await expect(page.getByRole("link", { name: "Back to video" })).toHaveAttribute(
    "href",
    "/plan",
  );

  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-receipt",
    "Saved · rendering started",
  );

  await page.getByRole("button", { name: "Play video" }).click();
  await expect(page.getByRole("button", { name: "Pause video" })).toBeVisible();
  await page.getByRole("button", { name: "Pause video" }).click();

  const firstClip = page.getByRole("button", { name: /Clip 1,/ });
  const initialWidth = (await firstClip.boundingBox())?.width ?? 0;
  await page.getByRole("button", { name: "Zoom timeline in" }).click();
  await expect
    .poll(async () => (await firstClip.boundingBox())?.width ?? 0)
    .toBeGreaterThan(initialWidth);
  await page.getByRole("button", { name: "Fit timeline" }).click();

  // Playback can advance by a device-dependent fraction before Pause lands.
  // Reset the media clock to the first-frame boundary before its reason check;
  // the selected clip's 44px trim handle deliberately owns the left-edge tap.
  await page.locator("video").evaluate((video: HTMLVideoElement) => {
    video.currentTime = 0;
    video.dispatchEvent(new Event("timeupdate"));
  });
  const split = page.getByRole("button", { name: "Split" });
  await expect(split).toHaveAttribute("aria-disabled", "true");
  await split.evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-receipt",
    "Move the playhead inside the selected clip to split",
  );

  await page
    .getByRole("button", { name: /Clip 2,/ })
    .click({ position: { x: 55, y: 24 } });
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
  await split.click();
  expect((await qaData<QaWindow[]>(page, "data-windows"))).toHaveLength(4);

  await page.getByRole("button", { name: "Mute" }).click();
  await expect(page.getByRole("button", { name: "Unmute" })).toBeVisible();
  await page.getByRole("button", { name: "Unmute" }).click();

  await page.getByRole("button", { name: "Delete" }).click();
  expect((await qaData<QaWindow[]>(page, "data-windows"))).toHaveLength(3);
  await page.getByRole("button", { name: "Undo" }).click();
  expect((await qaData<QaWindow[]>(page, "data-windows"))).toHaveLength(4);
});

test("precision sheet trims and applies Look and transition choices", async ({ page }) => {
  await page.getByRole("button", { name: "Adjust" }).click();
  const dialog = page.getByRole("dialog", { name: "Clip 1 adjustments" });
  await expect(dialog).toBeVisible();

  const before = await qaData<QaWindow[]>(page, "data-windows");
  await dialog.getByRole("button", { name: "In +0.1s" }).click();
  const after = await qaData<QaWindow[]>(page, "data-windows");
  expect(after[0].inS).toBeCloseTo(before[0].inS + 0.1, 6);
  expect(after[0].durationS).toBeCloseTo(before[0].durationS - 0.1, 6);

  await dialog.getByRole("tab", { name: "Look" }).click();
  await dialog.getByRole("button", { name: "Film" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-look", "Film");

  await dialog.getByRole("tab", { name: "Transition" }).click();
  await dialog.getByRole("button", { name: "Dissolve" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-transition",
    "Dissolve",
  );
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
});

test("Text edits directly on the preview and drags in one undoable gesture", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Text tool" }).click();
  await expect(page.getByTestId("mobile-tool-panel")).toHaveAttribute(
    "data-tool",
    "text",
  );
  await expect(page.getByRole("tab", { name: "Style" })).toBeVisible();
  const content = page.getByRole("textbox", { name: "Text content" });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(content).toBeFocused();
  await page.getByRole("button", { name: /Text font:/ }).click();
  await page
    .getByRole("option", { name: "Playfair Display", exact: true })
    .click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-font",
    "Playfair Display",
  );
  await expect(page.getByTestId("qa-text-overlay")).toHaveText("Add a title");
  const textLane = page.getByTestId("pocket-timeline-lane-text");
  await expect(textLane).toBeAttached();
  const textTimelineBar = textLane.getByRole("button", {
    name: /Text, Add a title/,
  });
  await expect(textTimelineBar).toHaveAttribute("aria-pressed", "true");

  await content.click();
  await content.fill("Three cities, one summer");
  await expect(page.getByTestId("qa-text-overlay")).toHaveText(
    "Three cities, one summer",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text",
    "Three cities, one summer",
  );
  await content.fill("");
  await expect(page.getByTestId("qa-text-overlay")).toBeEmpty();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-text", "");
  await content.fill("Three cities, one summer");
  const frame = page.getByTestId("qa-text-frame");
  const moveText = page.getByRole("button", { name: "Move text" });
  const preview = page.getByTestId("qa-preview-canvas");
  const xBefore = await qaNumber(page, "data-text-x");
  const yBefore = await qaNumber(page, "data-text-y");
  const historyBeforeDrag = await qaNumber(page, "data-history-len");
  await dragOnTarget(content, 24, 0);
  expect(await qaNumber(page, "data-text-x")).toBe(xBefore);
  expect(await qaNumber(page, "data-text-y")).toBe(yBefore);
  expect(await qaNumber(page, "data-history-len")).toBe(historyBeforeDrag);

  await dragOnTarget(moveText, 800, 1200);
  expect(await qaNumber(page, "data-text-x")).toBeGreaterThan(xBefore);
  expect(await qaNumber(page, "data-text-y")).toBeGreaterThan(yBefore);
  expect(await qaNumber(page, "data-history-len")).toBe(
    historyBeforeDrag + 1,
  );

  const frameBox = await frame.boundingBox();
  const previewBox = await preview.boundingBox();
  expect(frameBox).not.toBeNull();
  expect(previewBox).not.toBeNull();
  expect(frameBox!.x).toBeGreaterThanOrEqual(previewBox!.x);
  expect(frameBox!.x + frameBox!.width).toBeLessThanOrEqual(
    previewBox!.x + previewBox!.width,
  );
  expect(frameBox!.y).toBeGreaterThanOrEqual(previewBox!.y);
  expect(frameBox!.y + frameBox!.height).toBeLessThanOrEqual(
    previewBox!.y + previewBox!.height,
  );

  await page.getByRole("button", { name: "Undo" }).click();
  expect(await qaNumber(page, "data-text-x")).toBeCloseTo(xBefore, 4);
  expect(await qaNumber(page, "data-text-y")).toBeCloseTo(yBefore, 4);
  await expect(page.getByTestId("qa-text-overlay")).toHaveText(
    "Three cities, one summer",
  );

  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("qa-text-overlay")).toHaveText("Add a title");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text",
    "Add a title",
  );
});

test("a timed lane block resizes from both edges as one undoable gesture", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Text tool" }).click();

  const state = page.locator("#qa-state");
  const left = page.getByRole("button", { name: /Resize Add a title start/ });
  const right = page.getByRole("button", { name: /Resize Add a title end/ });
  const initialStart = await qaNumber(page, "data-text-start");
  const initialEnd = await qaNumber(page, "data-text-end");
  const initialHistory = await qaNumber(page, "data-history-len");
  const timeline = page.getByTestId("pocket-timeline-viewport");
  const initialScrollLeft = await timeline.evaluate((element) => element.scrollLeft);
  const initialPlayheadTime = await qaNumber(page, "data-current-time");

  const leftBox = await left.boundingBox();
  const rightBox = await right.boundingBox();
  expect(leftBox?.width).toBeGreaterThanOrEqual(44);
  expect(leftBox?.height).toBeGreaterThanOrEqual(44);
  expect(rightBox?.width).toBeGreaterThanOrEqual(44);
  expect(rightBox?.height).toBeGreaterThanOrEqual(44);

  await dragOnTarget(left, 48);
  expect(await qaNumber(page, "data-text-start")).toBeCloseTo(
    initialStart + 1,
    4,
  );
  expect(await qaNumber(page, "data-text-end")).toBeCloseTo(initialEnd, 4);
  expect(await qaNumber(page, "data-history-len")).toBe(initialHistory + 1);
  expect(await timeline.evaluate((element) => element.scrollLeft)).toBeCloseTo(
    initialScrollLeft,
    4,
  );
  expect(await qaNumber(page, "data-current-time")).toBeCloseTo(
    initialPlayheadTime,
    4,
  );

  await page.getByRole("button", { name: "Undo" }).click();
  await expect(state).toHaveAttribute("data-text-start", String(initialStart));
  await expect(state).toHaveAttribute("data-text-end", String(initialEnd));

  const historyBeforeRight = await qaNumber(page, "data-history-len");
  await dragOnTarget(right, -48);
  expect(await qaNumber(page, "data-text-start")).toBeCloseTo(initialStart, 4);
  expect(await qaNumber(page, "data-text-end")).toBeCloseTo(initialEnd - 1, 4);
  expect(await qaNumber(page, "data-history-len")).toBe(historyBeforeRight + 1);
  expect(await timeline.evaluate((element) => element.scrollLeft)).toBeCloseTo(
    initialScrollLeft,
    4,
  );
  expect(await qaNumber(page, "data-current-time")).toBeCloseTo(
    initialPlayheadTime,
    4,
  );

  await page.getByRole("button", { name: "Undo" }).click();
  await expect(state).toHaveAttribute("data-text-start", String(initialStart));
  await expect(state).toHaveAttribute("data-text-end", String(initialEnd));
});

test("lane resize auto-pans only when the dragged edge reaches the screen boundary", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Text tool" }).click();
  const state = page.locator("#qa-state");
  const timeline = page.getByTestId("pocket-timeline-viewport");
  const right = page.getByRole("button", { name: /Resize Add a title end/ });

  // Put the edge comfortably inside the phone before testing boundary follow.
  await dragOnTarget(right, -360);
  expect(await qaNumber(page, "data-text-end")).toBeCloseTo(3, 4);
  const initialScrollLeft = await timeline.evaluate((element) => element.scrollLeft);
  const initialPlayheadTime = await qaNumber(page, "data-current-time");
  const historyBefore = await qaNumber(page, "data-history-len");
  const viewportBox = await timeline.boundingBox();
  const handleBox = await right.boundingBox();
  if (!viewportBox || !handleBox) throw new Error("Lane boundary geometry is missing");
  const pointerId = 91;
  const y = handleBox.y + handleBox.height / 2;
  const dispatch = async (type: string, clientX: number) => {
    await right.evaluate(
      (element, event) => {
        element.dispatchEvent(
          new PointerEvent(event.type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: event.clientX,
            clientY: event.clientY,
            pointerId: event.pointerId,
            pointerType: "touch",
            isPrimary: true,
          }),
        );
      },
      { type, clientX, clientY: y, pointerId },
    );
  };

  await dispatch("pointerdown", handleBox.x + handleBox.width / 2);
  await dispatch("pointermove", viewportBox.x + viewportBox.width - 8);
  await page.waitForTimeout(120);
  expect(await timeline.evaluate((element) => element.scrollLeft)).toBeCloseTo(
    initialScrollLeft,
    4,
  );

  await dispatch("pointermove", viewportBox.x + viewportBox.width - 0.5);
  await page.waitForTimeout(220);
  const followedScrollLeft = await timeline.evaluate(
    (element) => element.scrollLeft,
  );
  expect(followedScrollLeft).toBeGreaterThan(initialScrollLeft + 16);
  await dispatch("pointerup", viewportBox.x + viewportBox.width - 0.5);

  expect(await qaNumber(page, "data-text-end")).toBeGreaterThan(7.5);
  expect(await qaNumber(page, "data-current-time")).toBeCloseTo(
    initialPlayheadTime,
    4,
  );
  expect(await qaNumber(page, "data-history-len")).toBe(historyBefore + 1);
  await expect(state).toHaveAttribute("data-text-start", "0");
});

test("text leaves the preview at its resized end time", async ({ page }) => {
  await page.getByRole("button", { name: "Text tool" }).click();
  await expect(page.getByTestId("qa-text-frame")).toBeVisible();

  const right = page.getByRole("button", { name: /Resize Add a title end/ });
  await dragOnTarget(right, -480);
  expect(await qaNumber(page, "data-text-end")).toBeCloseTo(0.5, 4);

  const timeline = page.getByTestId("pocket-timeline-viewport");
  const box = await timeline.boundingBox();
  if (!box) throw new Error("Timeline viewport is not visible");
  await page.mouse.click(box.x + 22 + 48, box.y + 24);
  await expect.poll(() => qaNumber(page, "data-current-time")).toBeCloseTo(1, 2);
  await expect(page.getByTestId("qa-text-frame")).toBeHidden();
});

test("captions leave the preview at their resized end time", async ({ page }) => {
  await expect(page.getByTestId("qa-caption-overlay")).toBeVisible();

  const captionLane = page.getByTestId("pocket-timeline-lane-captions");
  await captionLane.getByRole("button", { name: /Captions, Three cities/ }).click();
  const right = captionLane.locator(
    '[data-pocket-lane-resize-handle="right"]',
  );
  await dragOnTarget(right, -120);

  const timeline = page.getByTestId("pocket-timeline-viewport");
  const box = await timeline.boundingBox();
  if (!box) throw new Error("Timeline viewport is not visible");
  await page.mouse.click(box.x + 22 + 96, box.y + 24);
  await expect.poll(() => qaNumber(page, "data-current-time")).toBeCloseTo(2, 2);
  await expect(page.getByTestId("qa-caption-overlay")).toBeHidden();
});

test("Text exposes the complete production font, size, motion, and advanced controls", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Text tool" }).click();

  const textFrame = page.getByTestId("qa-text-frame");
  const initialFontFamily = await textFrame.evaluate(
    (element) => getComputedStyle(element).fontFamily,
  );
  await page.getByRole("button", { name: /Text font:/ }).click();
  const fontOptions = page
    .getByRole("listbox", { name: "Fonts" })
    .getByRole("option");
  await expect(fontOptions).toHaveCount(PRODUCTION_FONT_NAMES.length);
  expect((await fontOptions.allTextContents()).map((name) => name.trim())).toEqual(
    PRODUCTION_FONT_NAMES,
  );
  await page.getByRole("option", { name: "ZT Bros Oskon 90s" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-font",
    "ZT Bros Oskon 90s",
  );
  const selectedFontFamily = await textFrame.evaluate(
    (element) => getComputedStyle(element).fontFamily,
  );
  expect(selectedFontFamily).not.toBe(initialFontFamily);
  expect(selectedFontFamily).toContain("ZT Bros Oskon 90s");
  await expect
    .poll(() =>
      page.evaluate(async () => {
        await document.fonts.load("16px 'ZT Bros Oskon 90s'");
        return document.fonts.check("16px 'ZT Bros Oskon 90s'");
      }),
    )
    .toBe(true);

  const fineSize = page.getByRole("slider", { name: "Font size (fine)" });
  await expect(fineSize).toHaveAttribute("aria-valuemin", "8");
  await expect(fineSize).toHaveAttribute("aria-valuemax", "300");
  await page.getByRole("combobox", { name: "Font size" }).click();
  await page.getByRole("option", { name: "300", exact: true }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-text-size", "300");
  await expect(textFrame).toHaveCSS("font-size", "96px");

  await page.getByRole("tab", { name: "Motion" }).click();
  await page.getByRole("combobox", { name: "Animation" }).click();
  const animationOptions = page.getByRole("option");
  expect((await animationOptions.allTextContents()).map((name) => name.trim())).toEqual(
    PRODUCTION_TEXT_ANIMATIONS,
  );
  await page.getByRole("option", { name: "Handwriting" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-effect",
    "handwriting",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-motion",
    /"version":2/,
  );
  await expect(page.getByRole("slider", { name: "Speed" })).toBeVisible();

  await page.getByRole("combobox", { name: "Theme transition" }).click();
  await page.getByRole("option", { name: "Giant title wipe" }).click();
  await page.getByRole("textbox", { name: "Target glyph" }).fill("G");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-theme-transition",
    "giant-title-wipe",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-theme-target-glyph",
    "G",
  );

  await page.getByRole("tab", { name: "Advanced" }).click();
  await page.getByRole("combobox", { name: "Text case" }).click();
  await page.getByRole("option", { name: "Upper" }).click();
  await page.getByRole("spinbutton", { name: "Letter spacing" }).fill("0.12");
  await page.getByRole("spinbutton", { name: "Line spacing" }).fill("1.4");
  await page.getByRole("combobox", { name: "Shadow effect" }).click();
  await page.getByRole("option", { name: "High visibility" }).click();
  const stroke = page.getByRole("slider", { name: "Stroke width" });
  await stroke.focus();
  await stroke.press("ArrowRight");
  await expect(page.locator("#qa-state")).toHaveAttribute("data-text-case", "upper");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-letter-spacing",
    "0.12",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-line-spacing",
    "1.4",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-shadow",
    "high_visibility",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text-stroke-width",
    "1",
  );
});

test("every dock tool opens one inline shadcn control panel above the dock", async ({
  page,
}) => {
  const tools = ["Kria", "Captions", "Visuals", "Sounds", "Overlays", "Styles"] as const;
  for (const tool of tools) {
    await page.getByRole("button", { name: `${tool} tool` }).click();
    const panel = page.getByTestId("mobile-tool-panel");
    await expect(panel).toHaveAttribute(
      "data-tool",
      tool === "Kria" ? "nova" : tool.toLowerCase(),
    );
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const panelBox = await panel.boundingBox();
    const dockBox = await page.getByTestId("pocket-dock").boundingBox();
    expect(panelBox).not.toBeNull();
    expect(dockBox).not.toBeNull();
    expect(panelBox!.y + panelBox!.height).toBeLessThanOrEqual(dockBox!.y + 1);
    await page
      .getByRole("button", { name: `Close ${tool} controls` })
      .click();
    await expect(panel).toBeHidden();
  }
});

test("Sounds inserts SFX, changes music, and adjusts mix", async ({ page }) => {
  await page.getByRole("button", { name: "Sounds tool" }).click();
  await page.getByRole("button", { name: "Whoosh" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-sfx", '["Whoosh"]');
  await expect(page.getByTestId("pocket-timeline-lane-sfx")).toBeAttached();

  await page.getByRole("tab", { name: "Music" }).click();
  await page.getByRole("button", { name: "Golden Hour" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-music-track", "Golden Hour");
  await expect(
    page.getByTestId("pocket-timeline-lane-music").getByText("Golden Hour"),
  ).toBeAttached();

  await page.getByRole("tab", { name: "Mix" }).click();
  const slider = page.getByRole("slider", { name: "Music level" });
  const historyBeforeMix = await qaNumber(page, "data-history-len");
  await slider.focus();
  await slider.press("ArrowLeft");
  expect(await qaNumber(page, "data-music-gain")).toBe(69);
  expect(await qaNumber(page, "data-history-len")).toBe(historyBeforeMix + 1);
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-music-gain", "70");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-music-track",
    "Golden Hour",
  );
});

test("Captions edits copy, applies a style, and retranscribes", async ({ page }) => {
  await page
    .getByTestId("pocket-timeline-lane-captions")
    .getByRole("button", { name: /Captions, Three cities/ })
    .click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-selected-lane-id",
    "caption-cue-1",
  );
  await expect(page.getByTestId("mobile-tool-panel")).toHaveAttribute(
    "data-tool",
    "captions",
  );
  await page.getByRole("textbox", { name: "Caption cue text" }).fill("Meet me in Istanbul");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-caption",
    "Meet me in Istanbul",
  );
  await page.getByRole("tab", { name: "Style" }).click();
  await page.getByRole("button", { name: /Caption font:/ }).click();
  await page
    .getByRole("option", { name: "Playfair Display", exact: true })
    .click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-caption-font",
    "Playfair Display",
  );
  await page.getByRole("tab", { name: "Language" }).click();
  await page.getByRole("button", { name: "Turkish" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-caption-language",
    "Turkish",
  );
  await page.getByRole("button", { name: "Re-transcribe" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-caption-status",
    "Retranscribed",
  );
  await expect(page.getByTestId("qa-caption-overlay")).toHaveText(
    "Lisbon, Corfu, then Istanbul.",
  );
});

test("an uploaded visual renders the selected file in the preview", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Visuals tool" }).click();
  await page.getByLabel("Upload visual").setInputFiles({
    name: "uploaded-visual.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
      "base64",
    ),
  });

  const preview = page.getByTestId("qa-visual-preview");
  await expect(preview).toHaveAttribute("data-visual-label", "uploaded-visual.png");
  const image = preview.getByRole("img", { name: "uploaded-visual.png" });
  await expect(image).toBeVisible();
  await expect.poll(() => image.getAttribute("src")).toMatch(/^blob:/);
});

test("Visuals, overlays, and styles create visible editable state", async ({ page }) => {
  await page.getByRole("button", { name: "Visuals tool" }).click();
  await page.getByRole("button", { name: "Montage" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-visuals",
    '["3-shot montage"]',
  );
  await expect(page.getByTestId("pocket-timeline-lane-visuals")).toBeAttached();
  const visualPreview = page.getByTestId("qa-visual-preview");
  await expect(visualPreview).toBeVisible();
  await expect(visualPreview).toHaveAttribute("data-display-mode", "fullscreen");
  await expect(
    visualPreview.getByRole("img", { name: "3-shot montage" }),
  ).toBeVisible();
  const visualControls = page.getByTestId("mobile-tool-panel");
  await visualControls.getByRole("tab", { name: "Blocks" }).click();
  await visualControls.getByRole("button", { name: "Overlay" }).click();
  await expect(visualPreview).toHaveAttribute("data-display-mode", "overlay");
  await visualControls.getByRole("button", { name: "Fullscreen" }).click();
  await expect(visualPreview).toHaveAttribute("data-display-mode", "fullscreen");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-visuals",
    '["3-shot montage · Fullscreen"]',
  );
  const timeline = page.getByTestId("pocket-timeline-viewport");
  await timeline.evaluate((element) => {
    element.scrollLeft = 192;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await expect.poll(() => qaNumber(page, "data-current-time")).toBeCloseTo(4, 2);
  await expect(visualPreview).toBeHidden();

  await page.getByRole("button", { name: "Overlays tool" }).click();
  const historyBeforeUpload = await qaNumber(page, "data-history-len");
  await page.getByLabel("Upload overlay").setInputFiles({
    name: "travel-badge.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
      "base64",
    ),
  });
  const overlayPreview = page.getByTestId("qa-media-overlay");
  await expect(overlayPreview).toBeVisible();
  const overlayImage = overlayPreview.locator("img");
  await expect(overlayImage).toBeVisible();
  await expect.poll(() => overlayImage.getAttribute("src")).toMatch(/^blob:/);
  await expect(page.getByTestId("pocket-timeline-lane-overlays")).toBeAttached();
  expect(await qaNumber(page, "data-history-len")).toBe(
    historyBeforeUpload + 1,
  );
  await page.getByRole("tab", { name: "Place" }).click();
  const durationSlider = page.getByRole("slider", { name: "Overlay duration" });
  await durationSlider.focus();
  await durationSlider.press("Home");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-overlay",
    /"durationS":0.5/,
  );
  await page.getByRole("button", { name: "Right" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-overlay",
    /"position":"Right"/,
  );
  const scaleSlider = page.getByRole("slider", { name: "Overlay scale" });
  await scaleSlider.focus();
  await scaleSlider.press("Home");
  await expect(overlayPreview).toHaveAttribute("data-overlay-scale", "0.2");
  await page
    .getByTestId("mobile-tool-panel")
    .getByRole("button", { name: "Fullscreen", exact: true })
    .click();
  await expect(overlayPreview).toHaveAttribute("data-display-mode", "Fullscreen");
  await page.getByRole("button", { name: "Bring forward" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-overlay",
    /"zOrder":2/,
  );
  await page.getByRole("button", { name: "Styles tool" }).click();
  await page.getByRole("button", { name: "Film" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-look", "Film");
  await page.getByRole("tab", { name: "Clip" }).click();
  await page.getByRole("button", { name: "Warm" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-clip-look", "Warm");
  await page.getByRole("tab", { name: "Transition" }).click();
  await page.getByRole("button", { name: "Dissolve" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-transition", "Dissolve");
});

test("Kria review accepts an undoable proposed edit", async ({ page }) => {
  const before = await qaData<QaWindow[]>(page, "data-windows");
  await page.getByRole("button", { name: "Kria tool" }).click();
  await page.getByRole("button", { name: "Accept" }).click();
  const after = await qaData<QaWindow[]>(page, "data-windows");
  expect(after[0].durationS).toBeCloseTo(before[0].durationS - 0.2, 6);
  await expect(page.locator("#qa-state")).toHaveAttribute("data-kria-status", "Proposal applied");
  await page.getByRole("button", { name: "Undo" }).click();
  expect(await qaData<QaWindow[]>(page, "data-windows")).toEqual(before);

  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-kria-status",
    "Proposal rejected",
  );
});

test("Delete preserves one clip and exposes the reason without removing focus", async ({ page }) => {
  const deleteButton = page.getByRole("button", { name: "Delete" });
  await deleteButton.click();
  await deleteButton.click();
  expect((await qaData<QaWindow[]>(page, "data-windows"))).toHaveLength(1);
  await expect(deleteButton).toHaveAttribute("aria-disabled", "true");
  await deleteButton.focus();
  await expect(deleteButton).toBeFocused();
  await deleteButton.evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-receipt",
    "At least one clip must remain",
  );
  expect((await qaData<QaWindow[]>(page, "data-windows"))).toHaveLength(1);
});
