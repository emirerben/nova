import { expect, test } from "@playwright/test";
import {
  dispatchTouchPointer,
  expectNoHorizontalOverflow,
  installSyntheticTouchCapture,
  qaData,
  swipePageFromLocator,
} from "./mobile-helpers";

type QaCard = { id: string; start_s: number; end_s: number };
type PatchLog = { id: string; patch: Record<string, unknown>; record: boolean | null }[];

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/overlays");
  await installSyntheticTouchCapture(page);
});

test("overlays fixture has no horizontal overflow", async ({ page }) => {
  await expectNoHorizontalOverflow(page);
});

test("touch tap on the big chip opens a mobile-sized popover", async ({ page }) => {
  await dispatchTouchPointer(page, page.locator('[data-overlay-chip="big-card"]'), [
    { dx: 0, type: "pointerup" },
  ]);

  const popover = page.getByTestId("overlay-popover-big-card");
  await expect(popover).toBeVisible();

  const badInputs = await popover.locator("input").evaluateAll((inputs) =>
    inputs
      .map((input) => {
        const rect = input.getBoundingClientRect();
        const fontSize = Number.parseFloat(getComputedStyle(input).fontSize);
        return { height: rect.height, fontSize };
      })
      .filter(({ height, fontSize }) => height < 44 || fontSize < 16),
  );
  expect(badInputs).toEqual([]);
});

test("touch drag past slop moves a card without opening the popover and coalesces record flags", async ({ page }) => {
  const before = await qaData<QaCard[]>(page, "data-cards");

  await dispatchTouchPointer(page, page.locator('[data-overlay-chip="big-card"]'), [
    { dx: 80 },
    { dx: 110, type: "pointerup" },
  ]);

  await expect(page.getByTestId("overlay-popover-big-card")).toHaveCount(0);
  const after = await qaData<QaCard[]>(page, "data-cards");
  expect(after.find((card) => card.id === "big-card")?.start_s).not.toBe(
    before.find((card) => card.id === "big-card")?.start_s,
  );

  const log = await qaData<PatchLog>(page, "data-patch-log");
  expect(log.map((entry) => entry.record)).toEqual([true, false]);
});

test("cancelled sub-slop press neither opens the popover nor records patches", async ({ page }) => {
  await dispatchTouchPointer(page, page.locator('[data-overlay-chip="big-card"]'), [
    { dx: 2 },
    { dx: 2, type: "pointercancel" },
  ]);

  await expect(page.getByTestId("overlay-popover-big-card")).toHaveCount(0);
  expect(await qaData<PatchLog>(page, "data-patch-log")).toEqual([]);
});

test("vertical touch swipe on the overlay pan-y rail scrolls the page", async ({ page }) => {
  await swipePageFromLocator(page, page.locator(".touch-pan-y").first());
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
});
