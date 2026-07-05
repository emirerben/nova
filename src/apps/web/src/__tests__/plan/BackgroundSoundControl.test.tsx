import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("@/lib/plan-api", () => ({
  __esModule: true,
  setPlanItemNarratedBedLevel: jest.fn().mockResolvedValue(undefined),
}));

import BackgroundSoundControl from "@/app/plan/_components/BackgroundSoundControl";
import { setPlanItemNarratedBedLevel } from "@/lib/plan-api";

beforeEach(() => {
  jest.clearAllMocks();
});

function renderControl(overrides: Partial<React.ComponentProps<typeof BackgroundSoundControl>> = {}) {
  return render(
    <BackgroundSoundControl
      itemId="item-1"
      variantId="var-1"
      initialBedLevel={0.25}
      {...overrides}
    />,
  );
}

describe("BackgroundSoundControl", () => {
  it("does not commit on every onChange tick — only on release", () => {
    renderControl();
    const slider = screen.getByLabelText("Original video background sound level");

    fireEvent.change(slider, { target: { value: "0.5" } });
    fireEvent.change(slider, { target: { value: "0.6" } });
    fireEvent.change(slider, { target: { value: "0.7" } });

    // Three rapid drag ticks with no release yet — must not have committed.
    expect(setPlanItemNarratedBedLevel).not.toHaveBeenCalled();
  });

  it("commits once on pointer release with the latest value", () => {
    renderControl();
    const slider = screen.getByLabelText("Original video background sound level");

    fireEvent.change(slider, { target: { value: "0.5" } });
    fireEvent.change(slider, { target: { value: "0.8" } });
    fireEvent.pointerUp(slider);

    expect(setPlanItemNarratedBedLevel).toHaveBeenCalledTimes(1);
    expect(setPlanItemNarratedBedLevel).toHaveBeenCalledWith("item-1", "var-1", 0.8);
  });

  it("commits on key release too (keyboard drag path)", () => {
    renderControl();
    const slider = screen.getByLabelText("Original video background sound level");

    fireEvent.change(slider, { target: { value: "0.9" } });
    fireEvent.keyUp(slider);

    expect(setPlanItemNarratedBedLevel).toHaveBeenCalledWith("item-1", "var-1", 0.9);
  });

  it("falls back to the default level when initialBedLevel is null (Nova's default)", () => {
    renderControl({ initialBedLevel: null });
    const slider = screen.getByLabelText(
      "Original video background sound level",
    ) as HTMLInputElement;
    expect(Number(slider.value)).toBeCloseTo(0.25);
  });

  it("disables the slider while a render is in flight", () => {
    renderControl({ rendering: true });
    const slider = screen.getByLabelText("Original video background sound level");
    expect(slider).toBeDisabled();
    expect(screen.getByText("Applying…")).toBeInTheDocument();
  });

  it("surfaces an error message if the commit fails, without crashing", async () => {
    (setPlanItemNarratedBedLevel as jest.Mock).mockRejectedValueOnce(new Error("network down"));
    renderControl();
    const slider = screen.getByLabelText("Original video background sound level");

    fireEvent.change(slider, { target: { value: "0.4" } });
    fireEvent.pointerUp(slider);

    expect(await screen.findByText("network down")).toBeInTheDocument();
  });
});
