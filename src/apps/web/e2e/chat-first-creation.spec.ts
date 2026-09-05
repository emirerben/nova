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
      await expect(page.getByTestId("media-count")).toContainText("3 primary clips attached");
    }

    await page.goto(`${fixture}?state=upload`);
    await expect(page.getByLabel("Attach primary video clips")).toBeVisible();
    await expect(page.getByRole("button", { name: "Add visuals (optional)" })).toBeVisible();
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

  test("1280×720 keeps both desktop rails bounded to the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto(`${fixture}?state=ready`);

    const panes = await page.evaluate(() =>
      [".project-rail", ".chat-rail"].map((selector) => {
        const rect = document.querySelector<HTMLElement>(selector)!.getBoundingClientRect();
        return { height: rect.height, bottom: rect.bottom };
      }),
    );
    expect(panes).toEqual([
      { height: 720, bottom: 720 },
      { height: 720, bottom: 720 },
    ]);
    await expect(page.getByLabel("Message Kria")).toBeVisible();
  });

  test("authenticated canonical plan boots the real chat workspace", async ({ page }) => {
    const now = "2026-09-03T12:00:00Z";
    const thread = {
      id: "thread-e2e",
      status: "active",
      revision: 0,
      state: { edit_format: "montage", media: [], media_count: 0 },
      content_plan_id: "plan-e2e",
      active_plan_item_id: "item-e2e",
      active_creator_agent_session_id: null,
      active_job_id: null,
      media_capabilities: {
        clips: { current: 0, max: 50, server_max: 50, max_file_bytes: 2_000_000_000, content_types: ["video/mp4"], format: "montage" },
        visuals: { current: 0, max: 20, content_types: ["image/jpeg", "video/mp4"] },
        voiceover: { current: 0, max: 1, max_file_bytes: 500_000_000, content_types: ["audio/mpeg"] },
      },
      events: [{
        id: "event-e2e",
        sequence: 0,
        revision: 0,
        role: "assistant",
        event_type: "upload_prompt",
        content: "Add your clips, then tell me what the edit should feel like.",
        payload: { kind: "upload" },
        created_at: now,
      }],
      job: null,
      created_at: now,
      updated_at: now,
    };
    await page.route("**/api/auth/session", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ user: { name: "Launch Tester", email: "launch@example.com" }, expires: "2099-01-01T00:00:00Z" }),
    }));
    await page.route("**/api/plan/creation-threads/capabilities", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        formats: [
          { id: "montage", edit_format: "montage", max_clips: 50 },
          { id: "narrated", edit_format: "narrated_planned", max_clips: 50 },
          { id: "talking", edit_format: "subtitled", max_clips: 1 },
        ],
        media: thread.media_capabilities,
      }),
    }));
    await page.route("**/api/plan/creation-threads", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([thread]),
    }));
    await page.route("**/api/plan/creation-threads/thread-e2e", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(thread),
    }));

    await page.goto("/plan/thread-e2e");
    await expect(page).toHaveURL(/\/plan\/thread-e2e$/);
    await expect(page.getByTestId("project-title")).toBeVisible();
    await expect(page.getByTestId("project-title")).not.toHaveText("Create with Kria");
    await expect(page.getByLabel("Attach primary video clips")).toBeVisible();
    await page.getByRole("button", { name: "Change format" }).click();
    await expect(page.getByRole("button", { name: /^Montage/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Narrated/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Talking to camera/ })).toBeVisible();
  });

  test("200% zoom retains an actionable composer", async ({ page }) => {
    await page.goto(`${fixture}?state=ready`);
    await page.evaluate(() => { document.body.style.zoom = "2"; });
    await expect(page.getByLabel("Message Kria")).toBeVisible();
    await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  });

  test("canonical project deep-link reloads the named project", async ({ page }) => {
    await page.goto(`${fixture}?project=project-corfu&state=ready`);
    await expect(page.getByTestId("chat-first-creation-fixture")).toHaveAttribute("data-project-id", "project-corfu");
    await expect(page.getByTestId("chat-project-title")).toHaveText("Weekend in Corfu");
    await expect(page).toHaveURL(/project=project-corfu/);
    await page.reload();
    await expect(page.getByTestId("chat-project-title")).toHaveText("Weekend in Corfu");
  });

  test("renames a project and confirms deletion before removing it", async ({ page }) => {
    await page.goto(`${fixture}?state=ready`);
    await page.getByRole("button", { name: "Rename" }).click();
    await expect(page.getByRole("dialog", { name: "Rename project" })).toBeVisible();
    await page.getByLabel("Project name").fill("Corfu highlights");
    await page.getByRole("button", { name: "Save name" }).click();
    await expect(page.getByTestId("chat-project-title")).toHaveText("Corfu highlights");
    await expect(page.getByTestId("project-link")).toContainText("Corfu highlights");

    await page.getByRole("button", { name: "Delete" }).click();
    await expect(page.getByRole("dialog", { name: "Delete project?" })).toBeVisible();
    const deleteDialog = page.getByRole("dialog", { name: "Delete project?" });
    await expect(deleteDialog).toContainText("chat, uploads, edit data, and completed Kria videos");
    await expect(deleteDialog).toContainText("permanently removed and cannot be recovered");
    await expect(deleteDialog).toContainText("Published TikTok posts remain");
    await page.getByRole("button", { name: "Delete project" }).click();
    await expect(page.getByTestId("deleted-state")).toBeVisible();
    await expect(page.getByRole("region", { name: "Projects" })).toHaveCount(0);
  });

  test("keeps post-clip chat in chronological order", async ({ page }) => {
    await page.goto(`${fixture}?state=revision`);
    const clips = page.getByTestId("clips-section");
    const userMessage = page.getByTestId("post-clip-user-message");
    const assistantMessage = page.getByTestId("post-clip-assistant-message");
    const order = await page.evaluate(() => {
      const clips = document.querySelector("[data-testid='clips-section']")!;
      const user = document.querySelector("[data-testid='post-clip-user-message']")!;
      const assistant = document.querySelector("[data-testid='post-clip-assistant-message']")!;
      return {
        clipsBeforeUser: Boolean(clips.compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING),
        userBeforeAssistant: Boolean(user.compareDocumentPosition(assistant) & Node.DOCUMENT_POSITION_FOLLOWING),
      };
    });
    expect(order).toEqual({ clipsBeforeUser: true, userBeforeAssistant: true });
    await expect(clips).toBeVisible();
    await expect(userMessage).toBeVisible();
    await expect(assistantMessage).toBeVisible();
    await expect(page.getByTestId("latest-chat-anchor")).toBeVisible();
  });

  test("uses meaningful timed thinking states instead of generic first-second copy", async ({ page }) => {
    const cases = [
      ["0", "dots", ""],
      ["1499", "dots", ""],
      ["1500", "reading", "Reading your direction…"],
      ["7999", "reading", "Reading your direction…"],
      ["8000", "shaping", "Shaping the edit around your clips…"],
      ["19999", "shaping", "Shaping the edit around your clips…"],
      ["20000", "long", "Still working — your direction is saved."],
    ] as const;
    for (const [elapsed, tier, copy] of cases) {
      await page.goto(`${fixture}?state=thinking&elapsed=${elapsed}`);
      await expect(page.getByTestId("thinking-state")).toHaveAttribute("data-thinking-tier", tier);
      if (copy) {
        await expect(page.getByRole("status")).toContainText(copy);
      } else {
        await expect(page.getByRole("status", { name: "Kria is thinking" })).toHaveText("");
      }
    }
  });

  test("shows BeamLoader render progress and partial-result recovery", async ({ page }) => {
    await page.goto(`${fixture}?state=rendering`);
    await expect(page.getByTestId("render-progress")).toContainText("68%");
    await expect(page.locator(".beam-loader")).toHaveAttribute("data-mode", "line");
    await expect(page.getByRole("status", { name: "Rendering your video" })).toBeVisible();

    await page.goto(`${fixture}?state=partial`);
    await expect(page.getByTestId("partial-progress")).toContainText("Ready");
    await expect(page.getByRole("button", { name: "Retry variant" })).toBeVisible();
  });

  test("collapsed sidebar keeps the project title clear of its reveal control", async ({ page }) => {
    await page.goto(`${fixture}?state=ready`);
    await page.getByRole("button", { name: "Hide projects" }).click();
    await expect(page.getByTestId("show-projects")).toBeVisible();
    const reveal = await page.getByTestId("show-projects").boundingBox();
    const title = await page.getByTestId("chat-project-title").boundingBox();
    expect(reveal).not.toBeNull();
    expect(title).not.toBeNull();
    expect(reveal!.x + reveal!.width).toBeLessThanOrEqual(title!.x);
    await page.getByTestId("show-projects").click();
    await expect(page.getByRole("button", { name: "Hide projects" })).toBeVisible();
  });

  test("reduced motion disables fixture transitions and BeamLoader motion", async ({ page }) => {
    await page.goto(`${fixture}?state=rendering`);
    await page.getByTestId("reduced-motion-toggle").evaluate((button) => (button as HTMLButtonElement).click());
    await expect(page.locator("main")).toHaveClass(/chat-fixture-reduced-motion/);
    await expect(page.locator(".beam-loader__line")).toHaveCSS("animation-name", "none");
  });
});
