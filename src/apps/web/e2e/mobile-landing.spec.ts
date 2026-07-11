import { test } from "@playwright/test";
import { expectNoHorizontalOverflow } from "./mobile-helpers";

test("landing page has no horizontal overflow", async ({ page }) => {
  await page.goto("/");
  await expectNoHorizontalOverflow(page);
});
