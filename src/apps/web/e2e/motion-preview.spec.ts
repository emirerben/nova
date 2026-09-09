import { expect, test } from "@playwright/test";

test("maximum-complexity Creator preview stays within the calibrated draw budget", async ({ page }) => {
  await page.goto("/dev-qa/motion-preview");
  const state = page.locator("#qa-state");
  await expect(state).toHaveAttribute("data-status", "ready", { timeout: 30_000 });
  const drawCost = Number(await state.getAttribute("data-draw-cost-ms"));
  const calibrationCost = Number(await state.getAttribute("data-calibration-ms"));
  const ratio = Number(await state.getAttribute("data-draw-cost-ratio"));
  const ceiling = Number(await state.getAttribute("data-draw-cost-ceiling"));
  console.log({ drawCost, calibrationCost, ratio, ceiling });
  expect(Number.isFinite(drawCost)).toBe(true);
  expect(drawCost).toBeGreaterThan(0);
  expect(Number.isFinite(calibrationCost)).toBe(true);
  expect(calibrationCost).toBeGreaterThan(0);
  expect(Number.isFinite(ratio)).toBe(true);
  expect(ratio).toBeLessThanOrEqual(ceiling);
});

test("the calibrated budget catches a doubled draw workload", async ({ page }) => {
  await page.goto("/dev-qa/motion-preview?benchmark=2x");
  const state = page.locator("#qa-state");
  await expect(state).toHaveAttribute("data-status", "ready", { timeout: 30_000 });
  const multiplier = Number(await state.getAttribute("data-draw-multiplier"));
  const ratio = Number(await state.getAttribute("data-draw-cost-ratio"));
  const ceiling = Number(await state.getAttribute("data-draw-cost-ceiling"));
  console.log({ multiplier, ratio, ceiling });
  expect(multiplier).toBe(2);
  expect(ratio).toBeGreaterThan(ceiling);
});
