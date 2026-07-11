import { expect, type Locator, type Page } from "@playwright/test";

export async function expectNoHorizontalOverflow(page: Page) {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.innerWidth);
}

export async function qaData<T>(page: Page, attr: string): Promise<T> {
  const raw = await page.locator("#qa-state").getAttribute(attr);
  if (raw == null) throw new Error(`#qa-state missing ${attr}`);
  return JSON.parse(raw) as T;
}

export async function qaNumber(page: Page, attr: string): Promise<number> {
  const raw = await page.locator("#qa-state").getAttribute(attr);
  if (raw == null) throw new Error(`#qa-state missing ${attr}`);
  return Number(raw);
}

export async function installSyntheticTouchCapture(page: Page) {
  await page.evaluate(() => {
    HTMLElement.prototype.setPointerCapture = function setPointerCapture() {};
    HTMLElement.prototype.releasePointerCapture = function releasePointerCapture() {};
  });
}

export async function dispatchTouchPointer(
  page: Page,
  target: Locator,
  steps: { dx: number; dy?: number; type?: "pointermove" | "pointerup" | "pointercancel" }[],
) {
  const box = await target.boundingBox();
  if (!box) throw new Error("Target is not visible");
  const start = {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
  };
  const pointerId = 19;

  // Chromium rejects setPointerCapture for synthetic pointerIds because they
  // are not active native pointers. The fixture installs a no-op capture shim,
  // then we dispatch React-visible PointerEvents with pointerType=touch.
  await target.evaluate(
    (el, point) => {
      el.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          cancelable: true,
          composed: true,
          clientX: point.x,
          clientY: point.y,
          pointerId: 19,
          pointerType: "touch",
          isPrimary: true,
        }),
      );
    },
    start,
  );

  for (const step of steps) {
    await page.evaluate(
      ({ x, y, type }) => {
        window.dispatchEvent(
          new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: x,
            clientY: y,
            pointerId: 19,
            pointerType: "touch",
            isPrimary: true,
          }),
        );
      },
      {
        x: start.x + step.dx,
        y: start.y + (step.dy ?? 0),
        type: step.type ?? "pointermove",
      },
    );
  }
}

export async function swipePageFromLocator(page: Page, locator: Locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error("Swipe target is not visible");
  const client = await page.context().newCDPSession(page);
  const x = Math.round(box.x + box.width / 2);
  const y = Math.round(box.y + box.height / 2);

  await page.evaluate(() => window.scrollTo(0, 0));
  await client.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x, y }],
  });
  await client.send("Input.dispatchTouchEvent", {
    type: "touchMove",
    touchPoints: [{ x, y: Math.max(1, y - 180) }],
  });
  await client.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  await client.detach();
}
