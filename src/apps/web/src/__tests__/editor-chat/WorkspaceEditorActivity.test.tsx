import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import WorkspaceEditorActivity from "@/app/plan/_components/workspace/WorkspaceEditorActivity";
import type { EditorChatState } from "@/lib/editor-chat/protocol";
jest.mock("@/app/plan/items/[id]/_editor/DirectorSuggestions", () => ({ __esModule: true, default: () => null }));
const state = {
  messages: [], sending: false, confirmation: null, director: null,
  visuals: {
    phase: "ready", rows: [{ id: "suggestion", asset_id: "asset", confidence_tier: "confident", reason: "Shows the finished dish.", transcript_anchor: "ready to serve", overlay: { kind: "image", start_s: 65, end_s: 69, preview_url: "https://example.com/dish.jpg" }, sfx: { label: "Chime" } }],
    assets: [{ id: "asset", source_filename: "finished-dish.jpg", subject: "Dish", display_url: null }],
    wishlist: [], readyAssets: 1,
  },
} as unknown as EditorChatState;
test("visual approval identifies the asset, placement and sound before accepting", () => {
  const command = jest.fn();
  render(<WorkspaceEditorActivity state={state} onCommand={command} />);
  expect(screen.getByText("finished-dish.jpg")).toBeVisible();
  expect(screen.getByRole("img", { name: "Preview of finished-dish.jpg" })).toHaveAttribute("src", "https://example.com/dish.jpg");
  expect(screen.getByText("1:05–1:09")).toBeVisible();
  expect(screen.getByText("Includes Chime sound")).toBeVisible();
  expect(screen.getByText(/You say “ready to serve” here/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Add visual" }));
  expect(command).toHaveBeenCalledWith({ kind: "visuals-accept", suggestionId: "suggestion" });
  fireEvent.click(screen.getByRole("button", { name: "Show moment" }));
  expect(command).toHaveBeenLastCalledWith({ kind: "visuals-reveal", suggestionId: "suggestion" });
});
