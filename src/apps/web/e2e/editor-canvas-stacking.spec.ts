/**
 * KRI-8 — preview text must never paint (or hit-test) above editor chrome.
 *
 * jsdom can't compute stacking contexts, so this is the real-Chromium
 * behavioral guard: it drives the same dev-qa fixture as
 * editor-canvas-geometry.spec.ts, opted into a text layer parked at the
 * bottom of the frame plus a stand-in for one of EditorShell's two mutually
 * exclusive layout modes (`chrome=drawer` | `chrome=cta`), and asserts on
 * actual paint/hit order via `elementFromPoint`, not on declared class names.
 *
 * Root cause: EditorCanvas's private EDITOR_STAGE_Z scale (0-90) had no
 * stacking context to contain it, so it competed directly with EditorShell's
 * chrome z-indexes. Fix: `isolate` on the canvas root (EditorCanvas.tsx).
 */
import { expect, test, type Page } from "@playwright/test";

const BASE_QUERY = "hostWidth=900&hostHeight=700&canvas=portrait&zoom=100&stageHeightCss=420px&preview=virtual";
const url = (query: string) => `/dev-qa/editor-canvas-geometry?${query}`;

/** True if `target` is the topmost element at its own center. */
async function isOnTop(page: Page, targetTestId: string) {
  return page.getByTestId(targetTestId).evaluate((el: HTMLElement) => {
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    return hit === el || el.contains(hit);
  });
}

/**
 * The fixture parks each chrome probe exactly on top of the rendered text
 * layer (their geometric overlap is what the real bug needs to reproduce)
 * once its position-measuring effect settles.
 */
async function waitForProbePositioned(page: Page, targetTestId: string) {
  await page.waitForFunction((testid) => {
    const el = document.querySelector(`[data-testid="${testid}"]`) as HTMLElement | null;
    return el?.style.position === "absolute" && el.style.left !== "";
  }, targetTestId);
}

test("editor canvas root is an isolated stacking context", async ({ page }) => {
  await page.goto(url(`${BASE_QUERY}&chrome=cta&text=visible`));
  const isolation = await page
    .locator('[data-region="canvas"]')
    .evaluate((el) => getComputedStyle(el).isolation);
  expect(isolation).toBe("isolate");
});

test("a selected text layer never covers the tool drawer's Add text control", async ({ page }) => {
  // Selected + manipulable text takes the worst-case internal z-index
  // (EDITOR_STAGE_Z.selectionHandle), which used to beat the drawer's z-40.
  // Mirrors EditorShell's overlay layout, where the drawer is the only chrome
  // stand-in mounted alongside the canvas.
  await page.goto(url(`${BASE_QUERY}&chrome=drawer&text=selected`));
  await expect(page.getByTestId("chrome-add-text")).toBeVisible();
  await waitForProbePositioned(page, "chrome-add-text");
  expect(await isOnTop(page, "chrome-add-text")).toBe(true);
});

test("preview text never covers the floating Add text CTA (the reported screenshot)", async ({ page }) => {
  // The floating CTA carries no z-index at all, relying on DOM order + the
  // canvas being contained. This is the literal KRI-8 repro: "Michigan
  // Olympics Adventure" painting over the Add text button. Mirrors
  // EditorShell's light layout, where the floating CTA is the only chrome
  // stand-in mounted alongside the canvas.
  await page.goto(url(`${BASE_QUERY}&chrome=cta&text=visible`));
  await expect(page.getByTestId("chrome-floating-add-text")).toBeVisible();
  await waitForProbePositioned(page, "chrome-floating-add-text");
  expect(await isOnTop(page, "chrome-floating-add-text")).toBe(true);
});

test("preview text stays clipped inside the stage regardless of chrome overlap", async ({ page }) => {
  await page.goto(url(`${BASE_QUERY}&text=visible`));
  const stageRect = await page.getByTestId("editor-canvas-stage").boundingBox();
  const textRect = await page.getByText("Michigan Olympics Adventure").boundingBox();
  expect(stageRect).not.toBeNull();
  expect(textRect).not.toBeNull();
  if (!stageRect || !textRect) return;
  expect(textRect.x).toBeGreaterThanOrEqual(stageRect.x - 1);
  expect(textRect.y).toBeGreaterThanOrEqual(stageRect.y - 1);
  expect(textRect.x + textRect.width).toBeLessThanOrEqual(stageRect.x + stageRect.width + 1);
  expect(textRect.y + textRect.height).toBeLessThanOrEqual(stageRect.y + stageRect.height + 1);
});
