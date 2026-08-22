import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/dev-qa/guided-story-editor");
});

test("Guided Story V2 desktop flow preserves trim, edits transition/look, removes music, and commits tokens", async ({ page }) => {
  const state = page.locator("#qa-state");

  await expect(page.getByLabel("Clip 1 In")).toHaveValue("0.5");
  await expect(page.getByLabel("Clip 1 Out")).toHaveValue("5.5");

  await page.getByRole("button", { name: "Trim Clip 1 left to 1.0 seconds" }).click();
  await expect(page.getByLabel("Clip 1 In")).toHaveValue("1");
  await expect(page.getByLabel("Clip 1 Out")).toHaveValue("5.5");
  await expect(state).toHaveAttribute("data-clip-duration", "4.5");
  await expect(state).toHaveAttribute("data-past-len", "1");

  // One gesture creates one history entry and is reversible in one step.
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByLabel("Clip 1 In")).toHaveValue("0.5");
  await expect(state).toHaveAttribute("data-past-len", "0");
  await page.getByRole("button", { name: "Redo" }).click();
  await expect(page.getByLabel("Clip 1 In")).toHaveValue("1");
  await expect(state).toHaveAttribute("data-clip-duration", "4.5");

  await page.getByLabel("Clip 1 transition").selectOption("crossfade");
  await page.getByLabel("Clip 1 Look").selectOption("warm");
  await page.getByRole("button", { name: "Remove music" }).click();
  await expect(state).toHaveAttribute("data-transition", "crossfade");
  await expect(state).toHaveAttribute("data-look", "warm");
  await expect(state).toHaveAttribute("data-music-removed", "true");

  const lockedTrim = page.getByRole("button", { name: "Trim Clip 2 (voiceover locked) left to 1.0 seconds" });
  await expect(lockedTrim).toBeDisabled();
  await expect(page.getByTestId("disabled-operation-reason")).toContainText("locked to your voiceover");

  await page.getByRole("button", { name: "Save" }).click();
  await expect(state).toHaveAttribute("data-revision-id", "guided-story-fixture-revision-018");
  await expect(state).toHaveAttribute("data-generation-id", "guided-story-fixture-generation-018");

  await expect
    .poll(async () => JSON.parse((await state.getAttribute("data-commit-payload")) ?? "{}"))
    .toMatchObject({
      base_generation: "guided-story-fixture-generation-018",
      guided_revision_number: 18,
      remove_music: true,
      timeline_slots: [
        {
          in_s: 1,
          duration_s: 4.5,
          look_preset: "warm",
          transition_after: "crossfade",
        },
        {},
      ],
    });
});
