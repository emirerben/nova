import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/editor-timeline");
  await page.evaluate(() => window.localStorage.removeItem("nova-editor-timeline-media-scenario"));
  await page.reload();
});

test("13s clips plus an 8s middle Carousel share one 21s timeline", async ({ page }) => {
  await expect(page.locator("#qa-state")).toHaveAttribute("data-total-duration", "21");
  await expect(page.getByText("0:21")).toBeVisible();
  await expect(page.getByRole("button", { name: /Clip 3, timeline 0:14–0:16/ })).toBeVisible();

  const ruler = page.getByTestId("editor-timeline-ruler");
  const box = await ruler.boundingBox();
  if (!box) throw new Error("timeline ruler has no geometry");
  await page.mouse.click(box.x + box.width * 0.98, box.y + box.height / 2);
  await expect.poll(async () => Number(await page.locator("#qa-state").getAttribute("data-current-time"))).toBeGreaterThan(20);
});

test("both Carousel edges resize as one undoable gesture", async ({ page }) => {
  for (const side of ["right", "left"] as const) {
    const handle = page.locator(`[data-carousel-resize-handle="${side}"]`);
    const box = await handle.boundingBox();
    if (!box) throw new Error(`${side} resize handle has no geometry`);
    const direction = side === "right" ? 80 : -80;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + direction, box.y + box.height / 2);
    await page.mouse.up();
    await expect(page.locator("#qa-state")).toHaveAttribute("data-past-len", "1");
    await page.getByRole("button", { name: "Undo" }).click();
    await expect(page.locator("#qa-state")).toHaveAttribute("data-carousel-duration", "8");
  }
});

test("media rows keep overlapping z-order deterministic", async ({ page }) => {
  const media = page.locator('[data-editor-bar-kind="visual"]');
  await expect(media).toHaveCount(2);
  await expect(media.nth(0)).toHaveAttribute("data-editor-bar-id", "media-a");
  await expect(media.nth(0)).toHaveAttribute("data-editor-row-index", "0");
  await expect(media.nth(1)).toHaveAttribute("data-editor-bar-id", "media-b");
  await expect(media.nth(1)).toHaveAttribute("data-editor-row-index", "1");
});

test("video media right resize clamps to source duration in one undo snapshot", async ({ page }) => {
  const media = page.locator('[data-editor-bar-kind="visual"][data-editor-bar-id="media-a"]');
  const box = await media.boundingBox();
  if (!box) throw new Error("media-a has no geometry");

  await page.mouse.move(box.x + box.width - 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 160, box.y + box.height / 2);
  await page.mouse.up();

  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-first-end", "3");
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-past-len", "1");
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-past-len", "0");
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-first-end", "3");
});

test("place after selected uses the exact selected boundary and undoes once", async ({ page }) => {
  await page.getByRole("button", { name: "Place second media after first" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-second-start", "3");
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-past-len", "1");

  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-second-start", "2");
  await expect(page.locator("#qa-state")).toHaveAttribute("data-media-past-len", "0");
});

test("desktop unified-media journey survives save and reload", async ({ page }) => {
  const state = page.locator("#qa-state");

  await page.getByRole("button", { name: "Upload image and video" }).click();
  await expect(state).toHaveAttribute("data-uploads-ready", "true");
  await page.getByRole("button", { name: "Add uploaded image full screen" }).click();
  await expect(state).toHaveAttribute("data-display-mode", "fullscreen");

  await page.getByRole("button", { name: "Fit mode: Fit" }).click();
  await page.getByLabel("Media zoom").fill("1.6");
  await page.getByRole("button", { name: "Reposition focal point" }).click();
  await expect(state).toHaveAttribute("data-fit-mode", "cover");
  await expect(state).toHaveAttribute("data-zoom", "1.6");
  await expect(state).toHaveAttribute("data-focal-x", "0.8");

  await page.getByRole("button", { name: "Stack another image" }).click();
  await expect(state).toHaveAttribute("data-media-count", "3");

  const first = page.locator('[data-editor-bar-kind="visual"][data-editor-bar-id="media-a"]');
  const firstBox = await first.boundingBox();
  if (!firstBox) throw new Error("media-a has no geometry");
  await page.mouse.move(firstBox.x + firstBox.width - 2, firstBox.y + firstBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(firstBox.x + firstBox.width + 40, firstBox.y + firstBox.height / 2);
  await page.mouse.up();

  const resizedEnd = await state.getAttribute("data-media-first-end");
  await page.getByRole("button", { name: "Place second media after first" }).click();
  await expect(state).toHaveAttribute("data-media-second-start", resizedEnd ?? "");

  await page.getByRole("button", { name: "Bring first media to front" }).click();
  const frontZ = Number(await state.getAttribute("data-media-first-z"));
  expect(frontZ).toBeGreaterThan(3);
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(state).toHaveAttribute("data-media-second-start", resizedEnd ?? "");

  await page.getByRole("button", { name: "Save media edit" }).click();
  await page.reload();
  await expect(state).toHaveAttribute("data-reloaded-saved-state", "true");
  await expect(state).toHaveAttribute("data-media-count", "3");
  await expect(state).toHaveAttribute("data-fit-mode", "cover");
  await expect(state).toHaveAttribute("data-zoom", "1.6");
  await expect(state).toHaveAttribute("data-media-second-start", resizedEnd ?? "");
});
