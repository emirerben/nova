import { render, screen, waitFor } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
import "@testing-library/jest-dom";
import TikTokConnectionCard from "@/components/library/TikTokConnectionCard";
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

const mockedConnection = getTikTokConnection as jest.MockedFunction<typeof getTikTokConnection>;
const mockedStart = startTikTokOAuth as jest.MockedFunction<typeof startTikTokOAuth>;
const mockedDisconnect = disconnectTikTok as jest.MockedFunction<typeof disconnectTikTok>;
const mockedSync = syncTikTok as jest.MockedFunction<typeof syncTikTok>;

const partialConnection = {
  available: true,
  connected: true,
  status: "active",
  account: { display_name: "Nova Creator" },
  granted_scopes: ["user.info.basic", "video.publish"],
  can_publish: false,
  can_upload_draft: false,
  can_analyze: true,
  audited: false,
  beta: true,
  last_synced_at: null,
  learned_post_count: 3,
};

const fullyConnected = {
  ...partialConnection,
  granted_scopes: ["user.info.basic", "video.publish", "video.upload"],
  can_publish: true,
  can_upload_draft: true,
  audited: true,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedConnection.mockResolvedValue(partialConnection);
  mockedStart.mockResolvedValue();
});

it("surfaces a partial scope grant and offers reconnection", async () => {
  render(<TikTokConnectionCard />);

  expect(await screen.findByText("Partial access")).toBeInTheDocument();
  const user = userEvent.setup({ delay: null, pointerEventsCheck: PointerEventsCheckLevel.Never });
  await user.click(screen.getByRole("button", { name: "Reconnect" }));
  expect(mockedStart).toHaveBeenCalledTimes(1);
});

it("hides unavailable connections and reports the null state to the parent", async () => {
  const onConnection = jest.fn();
  mockedConnection.mockResolvedValue({
    ...partialConnection,
    available: false,
  });
  const { container } = render(<TikTokConnectionCard onConnection={onConnection} />);

  await waitFor(() => expect(onConnection).toHaveBeenCalledWith(expect.objectContaining({ available: false })));
  expect(container.innerHTML).toBe("");
});

it("shows a Connected badge and its overflow menu for a fully-connected account", async () => {
  mockedConnection.mockResolvedValue(fullyConnected);
  render(<TikTokConnectionCard />);

  expect(await screen.findByText("Connected")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "More TikTok actions" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
});

it("keeps the account connected when the disconnect AlertDialog is cancelled", async () => {
  mockedConnection.mockResolvedValue(fullyConnected);
  render(<TikTokConnectionCard />);

  const user = userEvent.setup({ delay: null, pointerEventsCheck: PointerEventsCheckLevel.Never });
  await user.click(await screen.findByRole("button", { name: "More TikTok actions" }));
  await user.click(await screen.findByRole("menuitem", { name: "Disconnect" }));

  const dialog = await screen.findByRole("alertdialog", { name: "Disconnect TikTok?" });
  expect(screen.getByText("Removes TikTok access from Kria. Your videos remain in Kria and on TikTok.")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Cancel" }));

  await waitFor(() => expect(dialog).not.toBeInTheDocument());
  expect(mockedDisconnect).not.toHaveBeenCalled();
});

it("disconnects TikTok after the AlertDialog is confirmed", async () => {
  mockedConnection.mockResolvedValue(fullyConnected);
  mockedDisconnect.mockResolvedValue();
  render(<TikTokConnectionCard />);

  const user = userEvent.setup({ delay: null, pointerEventsCheck: PointerEventsCheckLevel.Never });
  await user.click(await screen.findByRole("button", { name: "More TikTok actions" }));
  await user.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
  await screen.findByRole("alertdialog", { name: "Disconnect TikTok?" });
  await user.click(screen.getByRole("button", { name: "Disconnect" }));

  await waitFor(() => expect(mockedDisconnect).toHaveBeenCalledTimes(1));
});

it("syncs performance from the overflow menu and surfaces failures", async () => {
  mockedConnection.mockResolvedValue(fullyConnected);
  mockedSync.mockRejectedValue(new Error("TikTok is busy"));
  render(<TikTokConnectionCard />);

  const user = userEvent.setup({ delay: null, pointerEventsCheck: PointerEventsCheckLevel.Never });
  await user.click(await screen.findByRole("button", { name: "More TikTok actions" }));
  await user.click(await screen.findByRole("menuitem", { name: "Sync TikTok performance" }));

  expect(await screen.findByText("Kria couldn't sync TikTok performance. Try again.")).toBeInTheDocument();
  expect((screen.getByRole("button", { name: "More TikTok actions" }) as HTMLButtonElement).disabled).toBe(false);
});
