import { fireEvent, render, screen } from "@testing-library/react";
import TextMotionControls from "@/components/text-motion/TextMotionControls";
import { defaultTextMotion } from "@/lib/text-motion-v2";

describe("TextMotionControls", () => {
  it("commits a range once when the pointer gesture ends", () => {
    const onChange = jest.fn();
    const onPreview = jest.fn();
    const onBegin = jest.fn();
    render(
      <TextMotionControls
        effect="smooth-type"
        motion={defaultTextMotion("smooth-type")}
        onChange={onChange}
        onPreview={onPreview}
        onBegin={onBegin}
      />,
    );
    const speed = screen.getByLabelText("Speed");
    fireEvent.change(speed, { target: { value: "2" } });
    fireEvent.change(speed, { target: { value: "2.5" } });
    expect(onChange).not.toHaveBeenCalled();
    expect(onPreview).toHaveBeenCalledTimes(2);
    expect(onBegin).toHaveBeenCalledTimes(1);
    fireEvent.pointerUp(speed);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ speed: 2.5 });
  });

  it("shows only controls the selected effect implements", () => {
    render(
      <TextMotionControls
        effect="fade-in"
        motion={defaultTextMotion("fade-in")}
        onChange={jest.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Advanced motion"));
    expect(screen.getByLabelText("Motion easing")).toBeTruthy();
    expect(screen.getByLabelText("Hold")).toBeTruthy();
    expect(screen.queryByLabelText("Stagger")).toBeNull();
    expect(screen.queryByLabelText("Blur")).toBeNull();
  });

  it("exposes the complete Smooth Type customization set", () => {
    render(
      <TextMotionControls
        effect="smooth-type"
        motion={defaultTextMotion("smooth-type")}
        onChange={jest.fn()}
        onResetLegacy={jest.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Advanced motion"));
    for (const label of [
      "Speed",
      "Intensity",
      "Motion easing",
      "Stagger",
      "Reveal ramp",
      "Reveal order",
      "Entrance direction",
      "Travel",
      "Blur",
      "Hold",
    ]) {
      expect(screen.getByLabelText(label)).toBeTruthy();
    }
    expect(screen.getByText("Use legacy timing")).toBeTruthy();
  });
});
