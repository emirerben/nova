import { expect, test, type Locator } from "@playwright/test";
import {
  expectNoHorizontalOverflow,
  installSyntheticTouchCapture,
  qaData,
  qaNumber,
} from "./mobile-helpers";

type QaWindow = { id: string; inS: number; durationS: number };

async function dragOnTarget(target: Locator, dx: number) {
  const box = await target.boundingBox();
  if (!box) throw new Error("Timeline target is not visible");
  const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await target.evaluate(
    (element, point) => {
      for (const [type, x] of [
        ["pointerdown", point.x],
        ["pointermove", point.x + point.dx],
        ["pointerup", point.x + point.dx],
      ] as const) {
        element.dispatchEvent(
          new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: x,
            clientY: point.y,
            pointerId: 29,
            pointerType: "touch",
            isPrimary: true,
          }),
        );
      }
    },
    { ...start, dx },
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
  expect((await firstClip.boundingBox())?.width ?? 0).toBeGreaterThan(initialWidth);
  await page.getByRole("button", { name: "Fit timeline" }).click();

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

test("Text inserts on screen and focuses editing immediately", async ({ page }) => {
  await page.getByRole("button", { name: "Text tool" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit text" });
  const content = dialog.getByRole("textbox", { name: "Text content" });
  await expect(dialog).toBeVisible();
  await expect(content).toBeFocused();
  await expect(page.getByTestId("qa-text-overlay")).toHaveText("Add a title");

  await content.fill("Three cities, one summer");
  await expect(page.getByTestId("qa-text-overlay")).toHaveText(
    "Three cities, one summer",
  );
  await expect(page.locator("#qa-state")).toHaveAttribute(
    "data-text",
    "Three cities, one summer",
  );
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("pocket-dock")).toBeVisible();
  await expect(page.getByTestId("qa-text-overlay")).toHaveText(
    "Three cities, one summer",
  );
});

test("every remaining icon tool opens one shadcn sheet and its primary action works", async ({
  page,
}) => {
  const tools = [
    ["Kria", "Review proposal"],
    ["Captions", "Edit cue"],
    ["Visuals", "Add media"],
    ["Sounds", "Add SFX"],
    ["Overlays", "Upload overlay"],
    ["Styles", "Apply Look"],
  ] as const;

  for (const [tool, action] of tools) {
    await page.getByRole("button", { name: `${tool} tool` }).click();
    const dialog = page.getByRole("dialog", { name: tool });
    await expect(dialog).toBeVisible();
    expect(await page.getByRole("dialog").count()).toBe(1);
    await dialog.getByRole("button", { name: action }).click();
    await expect(page.locator("#qa-state")).toHaveAttribute(
      "data-tool-action",
      `${tool === "Kria" ? "nova" : tool.toLowerCase()}:${action}`,
    );
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();
  }
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
