import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/editor-timeline");
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
