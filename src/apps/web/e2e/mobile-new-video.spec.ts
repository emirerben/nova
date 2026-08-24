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

  const visibleCreateButtons = await page
    .getByRole("button", { name: "Create video" })
    .evaluateAll((buttons) =>
      buttons
        .map((button) => {
          const rect = button.getBoundingClientRect();
          const style = window.getComputedStyle(button);
          return {
            disabled: (button as HTMLButtonElement).disabled,
            height: rect.height,
            visible:
              rect.width > 0 &&
              rect.height > 0 &&
              style.display !== "none" &&
              style.visibility !== "hidden",
            width: rect.width,
            x: rect.x,
            y: rect.y,
          };
        })
        .filter((button) => button.visible),
    );
  expect(visibleCreateButtons).toHaveLength(1);
  expect(visibleCreateButtons[0].disabled).toBe(false);
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  expect(visibleCreateButtons[0].y).toBeGreaterThanOrEqual(0);
  expect(visibleCreateButtons[0].y + visibleCreateButtons[0].height).toBeLessThanOrEqual(
    viewport!.height,
  );
});
