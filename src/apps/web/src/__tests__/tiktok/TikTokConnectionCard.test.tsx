import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import TikTokConnectionCard from "@/app/library/_components/TikTokConnectionCard";
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

beforeEach(() => {
  jest.clearAllMocks();
  mockedConnection.mockResolvedValue({
    available: true,
    connected: true,
    status: "active",
    account: { display_name: "Nova Creator" },
    granted_scopes: ["user.info.basic", "video.list"],
    can_publish: false,
    can_analyze: true,
    audited: false,
    beta: true,
    last_synced_at: null,
    learned_post_count: 3,
  });
  mockedStart.mockResolvedValue();
});

it("surfaces a partial scope grant and offers reconnection", async () => {
  render(<TikTokConnectionCard />);

  expect(await screen.findByText(/granted partial access/i)).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
  expect(mockedStart).toHaveBeenCalledTimes(1);
});

it("hides unavailable connections and reports the null state to the parent", async () => {
  const onConnection = jest.fn();
  mockedConnection.mockResolvedValue({
    ...(await mockedConnection()),
    available: false,
  });
  const { container } = render(<TikTokConnectionCard onConnection={onConnection} />);

  await waitFor(() => expect(onConnection).toHaveBeenCalledWith(expect.objectContaining({ available: false })));
  expect(container.innerHTML).toBe("");
});

it("keeps the account connected when disconnect confirmation is cancelled", async () => {
  jest.spyOn(window, "confirm").mockReturnValue(false);
  render(<TikTokConnectionCard />);
  fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
  expect(mockedDisconnect).not.toHaveBeenCalled();
});

it("surfaces sync failures and re-enables the controls", async () => {
  mockedSync.mockRejectedValue(new Error("TikTok is busy"));
  render(<TikTokConnectionCard />);
  fireEvent.click(await screen.findByRole("button", { name: "Sync performance" }));
  expect(await screen.findByText("TikTok is busy")).not.toBeNull();
  expect((screen.getByRole("button", { name: "Sync performance" }) as HTMLButtonElement).disabled).toBe(false);
});
