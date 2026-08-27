import { expect, test, type Locator } from "@playwright/test";
import {
  expectNoHorizontalOverflow,
  installSyntheticTouchCapture,
  qaData,
  qaNumber,
} from "./mobile-helpers";

type QaWindow = { id: string; inS: number; durationS: number };

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
  const viewport = page.getByTestId("pocket-timeline-viewport");
  const playhead = page.getByTestId("pocket-ministrip-playhead");
  const viewportBox = await viewport.boundingBox();
  const playheadBox = await playhead.boundingBox();
  expect(viewportBox).not.toBeNull();
  expect(playheadBox).not.toBeNull();
  expect(Math.abs(playheadBox!.x - (viewportBox!.x + viewportBox!.width / 2))).toBeLessThan(2);

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
  expect(samples.length).toBeGreaterThan(0);
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
  await page.getByRole("button", { name: "Zoom timeline in" }).click();
  await expect(firstStrip).not.toHaveAttribute(
    "data-source-range-key",
    initialRangeKey!,
  );
  const nextRangeKey = await firstStrip.getAttribute("data-source-range-key");
  const renderedDuringDecode = await firstStrip.getAttribute(
    "data-rendered-range-key",
  );
  expect([null, nextRangeKey]).toContain(renderedDuringDecode);
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
  const content = page.getByRole("textbox", { name: "Text content" });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(content).toBeFocused();
  await expect(page.getByTestId("qa-text-overlay")).toHaveText("Add a title");

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
  const preview = page.getByTestId("qa-preview-canvas");
  const xBefore = await qaNumber(page, "data-text-x");
  const yBefore = await qaNumber(page, "data-text-y");
  const historyBeforeDrag = await qaNumber(page, "data-history-len");
  await dragOnTarget(frame, 800, 1200);
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

test("every remaining tool action opens a real editor state in one shadcn sheet", async ({
  page,
}) => {
  const tools = [
    ["Kria", ["Review proposal", "Accept edit", "Reject edit"]],
    ["Captions", ["Edit cue", "Style all", "Retranscribe"]],
    ["Visuals", ["Add media", "Add montage", "Add text card"]],
    ["Sounds", ["Add SFX", "Change music", "Adjust mix"]],
    ["Overlays", ["Upload overlay", "Adjust timing", "Position overlay"]],
    ["Styles", ["Apply Look", "Adjust clip Look", "Set transition"]],
  ] as const;

  for (const [tool, actions] of tools) {
    await page.getByRole("button", { name: `${tool} tool` }).click();
    await expect(page.getByRole("dialog", { name: tool })).toBeVisible();
    expect(await page.getByRole("dialog").count()).toBe(1);
    for (const action of actions) {
      await page
        .getByRole("dialog", { name: tool })
        .getByRole("button", { name: action, exact: true })
        .click();
      await expect(page.getByRole("dialog", { name: action })).toBeVisible();
      await expect(page.locator("#qa-state")).toHaveAttribute(
        "data-tool-action",
        `${tool === "Kria" ? "nova" : tool.toLowerCase()}:${action}`,
      );
      expect(await page.getByRole("dialog").count()).toBe(1);
      await page
        .getByRole("dialog", { name: action })
        .getByRole("button", { name: `Back to ${tool}` })
        .click();
    }
    const dialog = page.getByRole("dialog", { name: tool });
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();
  }
});

test("Sounds inserts SFX, changes music, and adjusts mix", async ({ page }) => {
  await page.getByRole("button", { name: "Sounds tool" }).click();
  await page.getByRole("dialog", { name: "Sounds" }).getByRole("button", { name: "Add SFX" }).click();
  await page.getByRole("dialog", { name: "Add SFX" }).getByRole("button", { name: "Whoosh" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-sfx", '["Whoosh"]');
  await page.getByRole("button", { name: "Back to Sounds" }).click();

  await page.getByRole("button", { name: "Change music" }).click();
  await page.getByRole("button", { name: "Golden Hour" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-music-track", "Golden Hour");
  await page.getByRole("button", { name: "Back to Sounds" }).click();

  await page.getByRole("button", { name: "Adjust mix" }).click();
  const slider = page.getByRole("slider", { name: "Music level" });
  const historyBeforeMix = await qaNumber(page, "data-history-len");
  await slider.focus();
  await slider.press("ArrowLeft");
  expect(await qaNumber(page, "data-music-gain")).toBe(69);
  expect(await qaNumber(page, "data-history-len")).toBe(historyBeforeMix + 1);
  await page.getByRole("button", { name: "Done" }).click();
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-music-gain", "70");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-music-track",
    "Golden Hour",
  );
});

test("Captions edits copy, applies a style, and retranscribes", async ({ page }) => {
  await page.getByRole("button", { name: "Captions tool" }).click();
  await page.getByRole("button", { name: "Edit cue" }).click();
  await page.getByRole("textbox", { name: "Caption cue text" }).fill("Meet me in Istanbul");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-caption",
    "Meet me in Istanbul",
  );
  await page.getByRole("button", { name: "Back to Captions" }).click();
  await page.getByRole("button", { name: "Style all" }).click();
  await page.getByRole("button", { name: "Lime" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-caption-style", "Lime");
  await page.getByRole("button", { name: "Back to Captions" }).click();
  await page.getByRole("button", { name: "Retranscribe" }).click();
  await page.getByRole("button", { name: "Retranscribe captions" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-caption-status",
    "Retranscribed",
  );
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("qa-caption-overlay")).toHaveText(
    "Lisbon, Corfu, then Istanbul.",
  );
});

test("Visuals, overlays, and styles create visible editable state", async ({ page }) => {
  await page.getByRole("button", { name: "Visuals tool" }).click();
  await page.getByRole("button", { name: "Add media" }).click();
  await page.getByRole("button", { name: "Bridge photo" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-visuals", '["Bridge photo"]');
  await page.getByRole("button", { name: "Back to Visuals" }).click();
  await page.getByRole("button", { name: "Add montage" }).click();
  await page.getByRole("button", { name: "Add 3-shot montage" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-visuals",
    '["Bridge photo","3-shot montage"]',
  );
  await page.getByRole("button", { name: "Back to Visuals" }).click();
  await page.getByRole("button", { name: "Add text card" }).click();
  await page.getByRole("button", { name: "Add text card" }).click();
  await expect(page.getByTestId("qa-text-overlay")).toHaveText(
    "Three cities, one summer",
  );

  await page.getByRole("button", { name: "Overlays tool" }).click();
  await page.getByRole("button", { name: "Upload overlay" }).click();
  const historyBeforeUpload = await qaNumber(page, "data-history-len");
  await page.getByLabel("Choose overlay file").setInputFiles({
    name: "travel-badge.png",
    mimeType: "image/png",
    buffer: Buffer.from("fixture-overlay"),
  });
  await expect(page.getByTestId("qa-media-overlay")).toHaveText(
    "travel-badge.png",
  );
  expect(await qaNumber(page, "data-history-len")).toBe(
    historyBeforeUpload + 1,
  );
  await page.getByRole("button", { name: "Use sample overlay" }).click();
  await expect(page.getByTestId("qa-media-overlay")).toHaveText("Nova travel badge");
  await page.getByRole("button", { name: "Back to Overlays" }).click();
  await page.getByRole("button", { name: "Adjust timing" }).click();
  const durationSlider = page.getByRole("slider", { name: "Overlay duration" });
  await durationSlider.focus();
  await durationSlider.press("Home");
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-overlay",
    /"durationS":0.5/,
  );
  await page.getByRole("button", { name: "Back to Overlays" }).click();
  await page.getByRole("button", { name: "Position overlay" }).click();
  await page.getByRole("button", { name: "Right" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-overlay",
    /"position":"Right"/,
  );
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("button", { name: "Styles tool" }).click();
  await page.getByRole("button", { name: "Apply Look" }).click();
  await page.getByRole("button", { name: "Film" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-look", "Film");
  await page.getByRole("button", { name: "Back to Styles" }).click();
  await page.getByRole("button", { name: "Adjust clip Look" }).click();
  await page.getByRole("button", { name: "Warm" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-clip-look", "Warm");
  await page.getByRole("button", { name: "Back to Styles" }).click();
  await page.getByRole("button", { name: "Set transition" }).click();
  await page.getByRole("button", { name: "Dissolve" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-transition", "Dissolve");
});

test("Kria review accepts an undoable proposed edit", async ({ page }) => {
  const before = await qaData<QaWindow[]>(page, "data-windows");
  await page.getByRole("button", { name: "Kria tool" }).click();
  await page.getByRole("button", { name: "Review proposal" }).click();
  await page.getByRole("button", { name: "Accept edit" }).click();
  const after = await qaData<QaWindow[]>(page, "data-windows");
  expect(after[0].durationS).toBeCloseTo(before[0].durationS - 0.2, 6);
  await expect(page.locator("#qa-state")).toHaveAttribute("data-kria-status", "Proposal applied");
  await page.getByRole("button", { name: "Undo" }).click();
  expect(await qaData<QaWindow[]>(page, "data-windows")).toEqual(before);

  await page.getByRole("button", { name: "Kria tool" }).click();
  await page.getByRole("button", { name: "Reject edit" }).click();
  await page.getByRole("button", { name: "Reject proposal" }).click();
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
