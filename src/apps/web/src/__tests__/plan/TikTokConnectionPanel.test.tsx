import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import TikTokConnectionPanel from "@/app/plan/_components/TikTokConnectionPanel";
import {
  disconnectTikTok,
  getTikTokConnection,
  startTikTokOAuth,
  syncTikTok,
} from "@/lib/tiktok-api";

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
    jest.mocked(disconnectTikTok).mockReset();
    jest.mocked(getTikTokConnection).mockResolvedValue(mockConnection);
    jest.mocked(startTikTokOAuth).mockResolvedValue(undefined);
    jest.mocked(syncTikTok).mockReset();
  });

  it("offers a canonical connection action and returns to its route after OAuth", async () => {
    const user = userEvent.setup();
    render(<TikTokConnectionPanel />);

    await user.click(await screen.findByRole("button", { name: "Connect TikTok" }));

    await waitFor(() => expect(startTikTokOAuth).toHaveBeenCalledWith("/plan/tiktok"));
    expect(screen.getByRole("region", { name: "TikTok connection" })).toHaveAttribute("id", "tiktok");
  });

  it("shows a retry action when loading the connection fails", async () => {
    jest.mocked(getTikTokConnection)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(mockConnection);
    const user = userEvent.setup();
    render(<TikTokConnectionPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t load/i);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Connect TikTok" })).toBeInTheDocument();
    expect(jest.mocked(getTikTokConnection).mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("explains when TikTok is unavailable for the account", async () => {
    jest.mocked(getTikTokConnection).mockResolvedValue({ ...mockConnection, available: false });
    render(<TikTokConnectionPanel />);

    expect(await screen.findByText(/not available for this account yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect TikTok" })).not.toBeInTheDocument();
  });

  it.each([
    ["reconnect-required status", { ...mockConnection, connected: true, status: "reconnect_required", granted_scopes: ["user.info.basic", "video.publish", "video.upload"] }],
    ["partial permissions", { ...mockConnection, connected: true, status: "connected", granted_scopes: ["user.info.basic"] }],
  ])("offers reconnect for %s", async (_label, value) => {
    jest.mocked(getTikTokConnection).mockResolvedValue(value);
    render(<TikTokConnectionPanel />);

    expect(await screen.findByRole("button", { name: "Reconnect TikTok" })).toBeInTheDocument();
    expect(screen.getByText(/reconnected before publishing/i)).toBeInTheDocument();
  });

  it("syncs and disconnects a fully connected account", async () => {
    jest.mocked(getTikTokConnection).mockResolvedValue({
      ...mockConnection,
      connected: true,
      status: "connected",
      granted_scopes: ["user.info.basic", "video.publish", "video.upload"],
      account: { display_name: "Emir" },
      can_analyze: true,
      last_synced_at: "2026-09-06T00:00:00Z",
    });
    jest.mocked(syncTikTok).mockResolvedValue({ status: "queued" });
    jest.mocked(disconnectTikTok).mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<TikTokConnectionPanel />);

    await user.click(await screen.findByRole("button", { name: "Sync performance" }));
    await waitFor(() => expect(syncTikTok).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await user.click(screen.getByRole("alertdialog").querySelector("button:last-child")!);
    await waitFor(() => expect(disconnectTikTok).toHaveBeenCalledTimes(1));
  });

  it("surfaces OAuth, sync, and disconnect errors", async () => {
    jest.mocked(startTikTokOAuth).mockRejectedValue(new Error("oauth"));
    const user = userEvent.setup();
    const first = render(<TikTokConnectionPanel />);
    await user.click(await screen.findByRole("button", { name: "Connect TikTok" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/access couldn.t start/i);
    first.unmount();

    jest.mocked(getTikTokConnection).mockResolvedValue({
      ...mockConnection,
      connected: true,
      status: "connected",
      granted_scopes: ["user.info.basic", "video.publish", "video.upload"],
      can_analyze: true,
    });
    jest.mocked(syncTikTok).mockRejectedValue(new Error("sync"));
    const view = render(<TikTokConnectionPanel />);
    await user.click(await view.findByRole("button", { name: "Sync performance" }));
    expect(await view.findByRole("alert")).toHaveTextContent(/couldn.t sync/i);

    jest.mocked(disconnectTikTok).mockRejectedValue(new Error("disconnect"));
    await user.click(view.getByRole("button", { name: "Disconnect" }));
    await user.click(view.getByRole("alertdialog").querySelector("button:last-child")!);
    expect(await view.findByRole("alert")).toHaveTextContent(/couldn.t disconnect/i);
  });
});
