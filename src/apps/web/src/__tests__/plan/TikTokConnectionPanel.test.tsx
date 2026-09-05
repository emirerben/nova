import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import TikTokConnectionPanel from "@/app/plan/_components/TikTokConnectionPanel";
import { getTikTokConnection, startTikTokOAuth } from "@/lib/tiktok-api";

jest.mock("@/lib/tiktok-api", () => ({
  getTikTokConnection: jest.fn(),
  startTikTokOAuth: jest.fn(),
  disconnectTikTok: jest.fn(),
  syncTikTok: jest.fn(),
}));

const mockConnection = {
  available: true,
  connected: false,
  status: "disconnected",
  account: null,
  granted_scopes: [],
  can_publish: false,
  can_upload_draft: false,
  can_analyze: false,
  audited: false,
  beta: true,
  last_synced_at: null,
  learned_post_count: 0,
};

describe("TikTokConnectionPanel", () => {
  beforeEach(() => {
    jest.mocked(getTikTokConnection).mockResolvedValue(mockConnection);
    jest.mocked(startTikTokOAuth).mockResolvedValue(undefined);
  });

  it("offers a canonical connection action and returns to its route after OAuth", async () => {
    const user = userEvent.setup();
    render(<TikTokConnectionPanel />);

    await user.click(await screen.findByRole("button", { name: "Connect TikTok" }));

    await waitFor(() => expect(startTikTokOAuth).toHaveBeenCalledWith("/plan/tiktok"));
    expect(screen.getByRole("region", { name: "TikTok connection" })).toHaveAttribute("id", "tiktok");
  });
});
