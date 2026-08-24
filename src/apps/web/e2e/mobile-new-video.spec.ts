import { expect, test } from "@playwright/test";
import { expectNoHorizontalOverflow } from "./mobile-helpers";

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/new-video-flow");
});

test("new-video flow has no horizontal overflow and keeps Generate reachable", async ({ page }) => {
  await expectNoHorizontalOverflow(page);

  const railState = await page.evaluate(() => {
    const styleRail = document.querySelector<HTMLElement>('[aria-label="Montage style"]');
    const kindCard = document.querySelector<HTMLElement>('[aria-label="What kind of video"] [data-layout="rail"]');
    const styleCards = Array.from(
      document.querySelectorAll<HTMLElement>('[aria-label="Montage style"] [data-layout="rail"]'),
    );
    const styleWidths = styleCards.map((el) => Math.round(el.getBoundingClientRect().width));
    return {
      styleRailIsScrollable: styleRail ? styleRail.scrollWidth > styleRail.clientWidth : false,
      styleCardCount: styleCards.length,
      kindCardWidth: kindCard ? Math.round(kindCard.getBoundingClientRect().width) : 0,
      styleWidths,
    };
  });
  expect(railState.styleRailIsScrollable).toBe(true);
  expect(railState.styleCardCount).toBeGreaterThan(1);
  expect(railState.kindCardWidth).toBeGreaterThanOrEqual(200);
  expect(railState.styleWidths.every((width) => width === railState.kindCardWidth)).toBe(true);

  await page.locator("#setup-title").scrollIntoViewIfNeeded();
  await expectNoHorizontalOverflow(page);

  const createVideo = page.getByRole("button", { name: "Create video" }).last();
  await expect(createVideo).toBeVisible();
  await expect(createVideo).toBeEnabled();
  const box = await createVideo.boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.height);
});
