import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/text-motion");
});

test("Smooth Type preview and detailed controls share committed state", async ({ page }) => {
  await expect(page.getByRole("heading", { name: "Smooth Type" })).toBeVisible();
  await expect(page.getByLabel("Speed")).toHaveValue("1");
  await expect(page.getByLabel("Intensity")).toHaveValue("70");
  const firstRevealLine = page.locator("[data-smooth-type-line]").first();
  await expect(firstRevealLine).toHaveCSS("clip-path", /inset/);

  await page.getByLabel("Preview time").fill("5.2");
  await expect(firstRevealLine).toHaveCSS("clip-path", "none");

  await page.getByText("Advanced motion").click();
  await expect(page.getByLabel("Motion easing")).toBeVisible();
  await expect(page.getByLabel("Reveal order")).toBeVisible();
  await expect(page.getByLabel("Entrance direction")).toBeVisible();
  await expect(page.getByLabel("Blur")).toBeVisible();

  await page.getByLabel("Speed").fill("2");
  await page.getByLabel("Speed").press("Tab");
  await page.getByLabel("Reveal order").click();
  await page.getByRole("option", { name: "Center out" }).click();
  await expect
    .poll(async () => JSON.parse((await page.locator("#qa-state").getAttribute("data-motion")) ?? "{}"))
    .toMatchObject({ speed: 2, order: "center-out", version: 2 });
});
