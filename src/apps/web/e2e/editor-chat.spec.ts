import { expect, test } from "@playwright/test";
import { installEditorChatFixture } from "./fixtures/editor-chat";

test.beforeEach(async ({ page }) => { await page.addInitScript(installEditorChatFixture); });

test("main chat edits the current draft twice, persists receipts, and never renders before Save", async ({ page }, testInfo) => {
  await page.goto("/plan/editor-chat-fixture");
  const editor = page.frameLocator('iframe[title="Full video editor"]');
  await expect(editor.getByRole("button", { name: /Text row 1, Original hook/ })).toBeVisible({ timeout: 20000 });
  await expect(editor.getByRole("textbox", { name: /Message/ })).toHaveCount(0);
  await editor.getByRole("button", { name: /Text row 1, Original hook/ }).click();
  await editor.getByRole("textbox", { name: "Text content" }).fill("Manually refined hook");
  await page.getByRole("textbox", { name: "Message Kria" }).fill("Update the opening text");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(editor.getByRole("button", { name: /Text row 1, Fresh morning in Italy/ })).toBeVisible({ timeout: 20000 });
  await expect(page.getByRole("button", { name: "Undo last edit" })).toBeVisible({ timeout: 20000 });
  await page.getByRole("textbox", { name: "Message Kria" }).fill("Use the second hook");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(editor.getByRole("button", { name: /Text row 1, Second hook/ })).toBeVisible({ timeout: 20000 });
  const requests = await page.evaluate(() => (window as any).editorChatFixture.requests);
  const turns = requests.filter((row: any) => row.path.endsWith("/copilot/turn"));
  expect(turns).toHaveLength(2);
  expect(turns[0].body.snapshot.text_bars[0].text).toBe("Manually refined hook");
  expect(turns[1].body.snapshot.text_bars[0].text).toBe("Fresh morning in Italy");
  expect(requests.filter((row: any) => /editor-commit|\/edit$|custom-effect|\/actions$|\/turns$/.test(row.path))).toHaveLength(0);
  await expect.poll(() => page.evaluate(() => (window as any).editorChatFixture.events.filter((event: any) => event.role === "assistant").length)).toBe(2);
  await page.getByRole("button", { name: "Undo last edit" }).click();
  await expect(editor.getByRole("button", { name: /Text row 1, Fresh morning in Italy/ })).toBeVisible({ timeout: 20000 });
  await page.screenshot({ path: testInfo.outputPath("unified-editor-chat.png") });
  await editor.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).editorChatFixture.requests.filter((row: any) => row.path.endsWith("/editor-commit")).length)).toBe(1);
  const saved = await page.evaluate(() => (window as any).editorChatFixture.requests.find((row: any) => row.path.endsWith("/editor-commit")).body);
  expect(saved.text_elements[0].text).toBe("Fresh morning in Italy");
  expect(saved.base_generation).toBe("generation-1");
});

test("direct links resolve the owning conversation and mobile tabs preserve the draft", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/plan/items/fixture-item/edit?variant=original_text");
  await expect(page).toHaveURL(/\/plan\/editor-chat-fixture$/);
  await page.getByRole("textbox", { name: "Message Kria" }).fill("Update the opening text");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.getByRole("button", { name: "Undo last edit" })).toBeVisible({ timeout: 20000 });
  await page.getByRole("tab", { name: "Editor", exact: true }).click();
  const editor = page.frameLocator('iframe[title="Full video editor"]');
  await expect(editor.getByRole("button", { name: /Text row 1, Fresh morning in Italy/ })).toBeVisible({ timeout: 20000 });
  await page.getByRole("tab", { name: "Chat", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Message Kria" })).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "Undo last edit" }).click();
  await page.getByRole("tab", { name: "Editor", exact: true }).click();
  await expect(editor.getByRole("button", { name: /Text row 1, Original hook/ })).toBeVisible({ timeout: 20000 });
});
