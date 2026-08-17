import { expect, test } from "@playwright/test";

test("maximum-complexity Creator preview has no repeated long tasks", async ({ page }) => {
  await page.goto("/dev-qa/motion-preview");
  const state = page.locator("#qa-state");
  await expect(state).toHaveAttribute("data-status", "ready", { timeout: 30_000 });
  const measured = Number(await state.getAttribute("data-measured-long-tasks"));
  const observed = Number(await state.getAttribute("data-observed-long-tasks"));
  expect(measured).toBeLessThanOrEqual(1);
  expect(observed).toBeLessThanOrEqual(1);
});
