import { expect, test } from "@playwright/test";

const fixture = "/dev-qa/chat-first-creation";

test.describe("Kria chat-first creation fixture", () => {
  test("desktop choose state exposes the three Paper formats and bounded panes", async ({ page }) => {
    await page.goto(`${fixture}?state=choose`);
    await expect(page.getByTestId("choose-state")).toBeVisible();
    await expect(page.getByRole("radio")).toHaveCount(3);
    await expect(page.getByRole("button", { name: /Add footage/ })).toBeVisible();

    const panes = await page.evaluate(() =>
      [".project-rail", ".chat-rail"].map((selector) => {
        const element = document.querySelector<HTMLElement>(selector)!;
        const rect = element.getBoundingClientRect();
        return { height: rect.height, width: rect.width };
      }),
    );
    expect(panes[0].width).toBe(260);
    expect(panes[0].height).toBe(900);
    expect(panes[1].height).toBe(900);
  });

  test("walks choose, upload, confirmation, render, ready, and editor states", async ({ page }) => {
    await page.goto(`${fixture}?state=choose`);
    const talking = page.locator('[data-format="talking"]');
    await talking.scrollIntoViewIfNeeded();
    await talking.click();
    await page.getByRole("button", { name: /Add footage/ }).click();
    await expect(page).toHaveURL(/state=upload/);
    await page.getByRole("button", { name: "Continue with footage" }).click();
    await expect(page.getByTestId("confirm-state")).toBeVisible();
    await page.getByRole("button", { name: "Confirm & render" }).click();
    await expect(page.getByTestId("rendering-state")).toBeVisible();

    await page.goto(`${fixture}?state=ready`);
    await expect(page.getByRole("button", { name: "Play cut" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Download" })).toBeVisible();
    await page.getByRole("button", { name: "Open editor" }).click();
    await expect(page.getByTestId("embedded-editor-canvas")).toBeVisible();
    await expect(page.getByText("Embedded EditorShell · overlay mode")).toBeVisible();
  });

  test("covers recovery artifacts and hydrates attached media count", async ({ page }) => {
    for (const state of ["upload-failed", "voiceover", "stale", "offline", "unavailable", "partial", "failed"]) {
      await page.goto(`${fixture}?state=${state}`);
      await expect(page.getByTestId(`${state}-state`)).toBeVisible();
      await expect(page.getByTestId("media-count")).toContainText("3 media items attached");
    }

    await page.goto(`${fixture}?state=upload`);
    await expect(page.getByLabel("Attach video, image, or audio")).toBeVisible();
  });

  test("projects, gallery, keyboard send, and reduced-motion controls work", async ({ page }) => {
    await page.goto(`${fixture}?state=ready`);
    await page.getByRole("button", { name: "Projects", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
    await page.getByRole("button", { name: /Back to chat/ }).click();
    await page.getByLabel("Creation conversation").getByRole("button", { name: "Gallery" }).click();
    await expect(page.getByRole("heading", { name: "Gallery" })).toBeVisible();
    await page.getByRole("button", { name: /Back to chat/ }).click();

    await page.getByLabel("Message Kria").fill("Hold the harbor shot longer");
    await page.getByLabel("Message Kria").press("Enter");
    await expect(page).toHaveURL(/state=revision/);
    await page.getByTestId("reduced-motion-toggle").evaluate((button) => (button as HTMLButtonElement).click());
    await expect(page.locator("main")).toHaveClass(/chat-fixture-reduced-motion/);
  });

  test("mobile uses a compact chat/editor surface without horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${fixture}?state=choose`);
    await expect(page.getByRole("radio")).toHaveCount(3);
    await expect(page.getByRole("button", { name: /Add footage/ })).toBeVisible();
    await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);

    await page.goto(`${fixture}?state=ready&view=editor`);
    await expect(page.getByTestId("embedded-editor-canvas")).toBeVisible();
    await expect(page.getByRole("button", { name: "Back to chat" })).toBeVisible();
  });

  test("200% zoom retains an actionable composer", async ({ page }) => {
    await page.goto(`${fixture}?state=ready`);
    await page.evaluate(() => { document.body.style.zoom = "2"; });
    await expect(page.getByLabel("Message Kria")).toBeVisible();
    await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  });
});
