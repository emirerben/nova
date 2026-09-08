import { expect, test, type Page } from "@playwright/test";

const url = (query: string) => `/dev-qa/editor-canvas-geometry?${query}`;

async function stageMetrics(page: Page) {
  return page.getByTestId("editor-canvas-stage").evaluate((stage: HTMLElement) => {
    const rect = stage.getBoundingClientRect();
    const viewport = stage.closest('[data-region="canvas"]');
    if (!viewport) throw new Error("canvas viewport missing");
    return {
      width: rect.width,
      height: rect.height,
      ratio: rect.width / rect.height,
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      viewportLeft: viewport.getBoundingClientRect().left,
      viewportTop: viewport.getBoundingClientRect().top,
      viewportRight: viewport.getBoundingClientRect().right,
      viewportBottom: viewport.getBoundingClientRect().bottom,
      viewportWidth: viewport.clientWidth,
      viewportHeight: viewport.clientHeight,
      scrollWidth: viewport.scrollWidth,
      scrollHeight: viewport.scrollHeight,
    };
  });
}

test("stage uses the selected output aspect ratio and stays inside the host at 100%", async ({ page }) => {
  await page.goto(url("hostWidth=900&hostHeight=700&canvas=portrait&zoom=100&stageHeightCss=420px"));
  const metrics = await stageMetrics(page);
  expect(metrics.ratio).toBeCloseTo(1080 / 1920, 3);
  expect(metrics.width).toBeGreaterThan(0);
  expect(metrics.height).toBeGreaterThan(0);
  expect(metrics.left).toBeGreaterThanOrEqual(metrics.viewportLeft);
  expect(metrics.top).toBeGreaterThanOrEqual(metrics.viewportTop);
  expect(metrics.right).toBeLessThanOrEqual(metrics.viewportRight);
  expect(metrics.bottom).toBeLessThanOrEqual(metrics.viewportBottom);
});

test("clean and virtual previews have identical stage geometry", async ({ page }) => {
  await page.goto(url("hostWidth=900&hostHeight=700&canvas=portrait&zoom=100&stageHeightCss=420px&preview=clean"));
  const clean = await stageMetrics(page);
  await page.goto(url("hostWidth=900&hostHeight=700&canvas=portrait&zoom=100&stageHeightCss=420px&preview=virtual"));
  const virtual = await stageMetrics(page);
  expect(virtual.width).toBeCloseTo(clean.width, 3);
  expect(virtual.height).toBeCloseTo(clean.height, 3);
  await expect(page.locator('[data-virtual-preview-deck="a"]')).toBeAttached();
});

test("clean and virtual portrait previews clamp identically in a 320px host", async ({ page }) => {
  await page.goto(url("hostWidth=320&hostHeight=700&canvas=portrait&zoom=100&stageHeightCss=600px&preview=clean"));
  const clean = await stageMetrics(page);
  await page.getByTestId("virtual-preview-control").click();
  await expect(page).toHaveURL(/hostWidth=320/);
  await expect(page).toHaveURL(/hostHeight=700/);
  await expect(page).toHaveURL(/canvas=portrait/);
  await expect(page).toHaveURL(/zoom=100/);
  await expect(page).toHaveURL(/stageHeightCss=600px/);
  await expect(page).toHaveURL(/preview=virtual/);
  await expect(page.getByTestId("editor-canvas-geometry-fixture")).toHaveAttribute("data-preview-mode", "virtual");
  await expect(page.locator('[data-virtual-preview-deck="a"]')).toBeAttached();
  const virtual = await stageMetrics(page);

  for (const metrics of [clean, virtual]) {
    expect(metrics.ratio).toBeCloseTo(1080 / 1920, 3);
    expect(metrics.left).toBeGreaterThanOrEqual(metrics.viewportLeft);
    expect(metrics.right).toBeLessThanOrEqual(metrics.viewportRight);
  }
  expect(virtual.width).toBeCloseTo(clean.width, 3);
  expect(virtual.height).toBeCloseTo(clean.height, 3);

  await page.getByTestId("clean-preview-control").click();
  await expect(page.getByTestId("editor-canvas-geometry-fixture")).toHaveAttribute("data-preview-mode", "clean");
  await expect.poll(async () => (await stageMetrics(page)).width).toBeCloseTo(clean.width, 3);
});

test("virtual landscape preview preserves 16:9 geometry in a constrained host", async ({ page }) => {
  await page.goto(url("hostWidth=320&hostHeight=700&canvas=landscape&zoom=100&stageHeightCss=420px&preview=virtual"));
  const metrics = await stageMetrics(page);

  expect(metrics.ratio).toBeCloseTo(1920 / 1080, 3);
  expect(metrics.width).toBeGreaterThan(0);
  expect(metrics.height).toBeGreaterThan(0);
  expect(metrics.left).toBeGreaterThanOrEqual(metrics.viewportLeft);
  expect(metrics.top).toBeGreaterThanOrEqual(metrics.viewportTop);
  expect(metrics.right).toBeLessThanOrEqual(metrics.viewportRight);
  expect(metrics.bottom).toBeLessThanOrEqual(metrics.viewportBottom);
  await expect(page.locator('[data-virtual-preview-deck="a"]')).toBeAttached();
});

test("150% and 200% zoom expand the scrollable canvas without changing its aspect ratio", async ({ page }) => {
  await page.goto(url("hostWidth=600&hostHeight=700&canvas=portrait&zoom=100&stageHeightCss=420px"));
  const baseline = await stageMetrics(page);

  for (const zoom of [150, 200]) {
    await page.goto(url(`hostWidth=600&hostHeight=700&canvas=portrait&zoom=${zoom}&stageHeightCss=420px`));
    const metrics = await stageMetrics(page);
    expect(metrics.ratio, `${zoom}%`).toBeCloseTo(1080 / 1920, 3);
    expect(metrics.width, `${zoom}% stage width`).toBeCloseTo(baseline.width * (zoom / 100), 2);
    expect(metrics.height, `${zoom}% stage height`).toBeCloseTo(baseline.height * (zoom / 100), 2);
    expect(metrics.scrollWidth, `${zoom}%`).toBeGreaterThan(metrics.viewportWidth);
    if (zoom === 200) {
      expect(metrics.scrollHeight, `${zoom}%`).toBeGreaterThan(metrics.viewportHeight);
    }
  }
});

test("supports portrait, landscape, fallback height, and short viewport floor", async ({ page }) => {
  await page.goto(url("hostWidth=1000&hostHeight=700&canvas=landscape&zoom=100&stageHeightCss=420px"));
  expect((await stageMetrics(page)).ratio).toBeCloseTo(1920 / 1080, 3);

  await page.setViewportSize({ width: 900, height: 300 });
  await page.goto(url("hostWidth=900&hostHeight=300&canvas=portrait&zoom=100&stageHeightCss=100vh - 398px"));
  const short = await stageMetrics(page);
  expect(short.width).toBeGreaterThan(0);
  expect(short.height).toBeGreaterThan(0);
  expect(short.left).toBeGreaterThanOrEqual(short.viewportLeft);
  expect(short.top).toBeGreaterThanOrEqual(short.viewportTop);
  expect(short.right).toBeLessThanOrEqual(short.viewportRight);
  expect(short.bottom).toBeLessThanOrEqual(short.viewportBottom);

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(url("hostWidth=900&hostHeight=700&canvas=portrait&zoom=100"));
  const fallback = await stageMetrics(page);
  expect(fallback.width).toBeGreaterThan(0);
  expect(fallback.height).toBeGreaterThan(0);
  expect(fallback.ratio).toBeCloseTo(1080 / 1920, 3);
  expect(fallback.left).toBeGreaterThanOrEqual(fallback.viewportLeft);
  expect(fallback.top).toBeGreaterThanOrEqual(fallback.viewportTop);
  expect(fallback.right).toBeLessThanOrEqual(fallback.viewportRight);
  expect(fallback.bottom).toBeLessThanOrEqual(fallback.viewportBottom);
});

test("keeps positive geometry for every editor shell height expression", async ({ page }) => {
  for (const stageHeightCss of [
    "46dvh - 128px",
    "100dvh - 398px",
    "100dvh - 350px",
    "100dvh - 152px",
  ]) {
    await page.goto(url(`hostWidth=1200&hostHeight=800&canvas=portrait&zoom=100&stageHeightCss=${encodeURIComponent(stageHeightCss)}`));
    const metrics = await stageMetrics(page);
    expect(metrics.width, stageHeightCss).toBeGreaterThan(0);
    expect(metrics.height, stageHeightCss).toBeGreaterThan(0);
    expect(metrics.ratio, stageHeightCss).toBeCloseTo(1080 / 1920, 3);
  }
});
