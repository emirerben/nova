import { expect, test } from "@playwright/test";
import {
  dispatchTouchPointer,
  expectNoHorizontalOverflow,
  installSyntheticTouchCapture,
  qaData,
  qaNumber,
  swipePageFromLocator,
} from "./mobile-helpers";

type QaSlot = { key: string; inS: number; durationS: number; removed: boolean };

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/clips");
  await installSyntheticTouchCapture(page);
});

test("clips fixture has no horizontal overflow and touch handles stay 44px in viewport", async ({ page }) => {
  await expectNoHorizontalOverflow(page);

  const failures = await page.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>("*"))
      .filter((el) => getComputedStyle(el).touchAction === "none")
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return {
          label: el.getAttribute("aria-label") ?? el.getAttribute("data-inline-trim-handle") ?? el.tagName,
          width: rect.width,
          height: rect.height,
          left: rect.left,
          right: rect.right,
        };
      })
      .filter(
        (rect) =>
          rect.width < 44 ||
          rect.height < 44 ||
          rect.left < 0 ||
          rect.right > window.innerWidth,
      ),
  );

  expect(failures).toEqual([]);
});

test("touch dragging the first right handle records one undo step and undo restores it", async ({ page }) => {
  const before = await qaData<QaSlot[]>(page, "data-slots");
  const beforePast = await qaNumber(page, "data-past-len");

  await dispatchTouchPointer(page, page.locator('[data-inline-trim-handle="right-s1"]'), [
    { dx: 80 },
    { dx: 110, type: "pointerup" },
  ]);

  const after = await qaData<QaSlot[]>(page, "data-slots");
  expect(after[0].durationS).not.toBe(before[0].durationS);
  expect(await qaNumber(page, "data-past-len")).toBe(beforePast + 1);

  await page.getByRole("button", { name: /undo/i }).click();
  const undone = await qaData<QaSlot[]>(page, "data-slots");
  expect(undone[0].durationS).toBe(before[0].durationS);
});

test("sub-slop touch press does not mutate clip state", async ({ page }) => {
  const before = await qaData<QaSlot[]>(page, "data-slots");
  const beforePast = await qaNumber(page, "data-past-len");

  await dispatchTouchPointer(page, page.locator('[data-inline-trim-handle="right-s1"]'), [
    { dx: 3 },
    { dx: 3, type: "pointerup" },
  ]);

  expect(await qaData<QaSlot[]>(page, "data-slots")).toEqual(before);
  expect(await qaNumber(page, "data-past-len")).toBe(beforePast);
});

test("pointercancel mid-drag commits the visible clip value once", async ({ page }) => {
  const before = await qaData<QaSlot[]>(page, "data-slots");

  await dispatchTouchPointer(page, page.locator('[data-inline-trim-handle="right-s1"]'), [
    { dx: 80 },
    { dx: 110, type: "pointercancel" },
  ]);

  const after = await qaData<QaSlot[]>(page, "data-slots");
  expect(after[0].durationS).not.toBe(before[0].durationS);
  expect(await qaNumber(page, "data-past-len")).toBe(1);
});

test("vertical touch swipe on the clips pan-y rail scrolls the page", async ({ page }) => {
  await swipePageFromLocator(page, page.locator('[class*="touch-action:pan-y"]').first());
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
});
