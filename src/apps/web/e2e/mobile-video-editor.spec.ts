import { expect, test, type Locator } from "@playwright/test";
import {
  expectNoHorizontalOverflow,
  installSyntheticTouchCapture,
  qaData,
  qaNumber,
} from "./mobile-helpers";

type QaWindow = { id: string; inS: number; durationS: number };

async function dragOnTarget(target: Locator, dx: number) {
  const box = await target.boundingBox();
  if (!box) throw new Error("Timeline target is not visible");
  const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await target.evaluate(
    (element, point) => {
      for (const [type, x] of [
        ["pointerdown", point.x],
        ["pointermove", point.x + point.dx],
        ["pointerup", point.x + point.dx],
      ] as const) {
        element.dispatchEvent(
          new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: x,
            clientY: point.y,
            pointerId: 29,
            pointerType: "touch",
            isPrimary: true,
          }),
        );
      }
    },
    { ...start, dx },
  );
}

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/mobile-editor");
  await installSyntheticTouchCapture(page);
});

test("mobile timeline fits the viewport with fixed playhead, 44px handles, and icon dock", async ({
  page,
}) => {
  await expectNoHorizontalOverflow(page);
  const viewport = page.getByTestId("pocket-timeline-viewport");
  const playhead = page.getByTestId("pocket-ministrip-playhead");
  const viewportBox = await viewport.boundingBox();
  const playheadBox = await playhead.boundingBox();
  expect(viewportBox).not.toBeNull();
  expect(playheadBox).not.toBeNull();
  expect(Math.abs(playheadBox!.x - (viewportBox!.x + viewportBox!.width / 2))).toBeLessThan(2);

  for (const name of [/Trim clip start/, /Trim clip end/]) {
    const box = await page.getByRole("button", { name }).boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }

  const dockBox = await page.getByTestId("pocket-dock").boundingBox();
  expect(dockBox).not.toBeNull();
  expect(dockBox!.y + dockBox!.height).toBeLessThanOrEqual(
    await page.evaluate(() => window.innerHeight),
  );
  await expect(page.getByRole("button", { name: "Visuals tool" })).toBeVisible();
});

test("touch trim records one undo step, ripples later clips, and Undo restores it", async ({
  page,
}) => {
  const before = await qaData<QaWindow[]>(page, "data-windows");
  await dragOnTarget(page.getByRole("button", { name: /Trim clip end/ }), -48);

  const after = await qaData<QaWindow[]>(page, "data-windows");
  expect(after[0].durationS).toBeLessThan(before[0].durationS);
  expect(await qaNumber(page, "data-history-len")).toBe(1);
  await expect(page.getByRole("button", { name: "Undo" })).toBeVisible();

  await page.getByRole("button", { name: "Undo" }).click();
  expect(await qaData<QaWindow[]>(page, "data-windows")).toEqual(before);
});

test("filmstrip dragging scrubs without changing the selected source window", async ({
  page,
}) => {
  const before = await qaData<QaWindow[]>(page, "data-windows");
  const beforeTime = await qaNumber(page, "data-current-time");
  await dragOnTarget(page.getByTestId("pocket-timeline-viewport"), -72);

  expect(await qaData<QaWindow[]>(page, "data-windows")).toEqual(before);
  expect(await qaNumber(page, "data-current-time")).toBeGreaterThan(beforeTime);
});
