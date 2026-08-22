import "@testing-library/jest-dom";
import { act, render, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";

describe("Toaster", () => {
  it("mounts a notifications region and renders a bottom-center ink toast", async () => {
    render(<Toaster />);
    // Sonner only renders the positioned [data-sonner-toaster] list once a
    // toast exists — assert the always-present live region first.
    expect(document.body.querySelector('[aria-label^="Notifications"]')).not.toBeNull();

    act(() => {
      toast("Saved");
    });

    await waitFor(() => {
      const region = document.body.querySelector("[data-sonner-toaster]");
      expect(region).not.toBeNull();
      expect(region).toHaveAttribute("data-x-position", "center");
      expect(region).toHaveAttribute("data-y-position", "bottom");
    });

    const toastEl = document.body.querySelector("[data-sonner-toast]");
    expect(toastEl).not.toBeNull();
    expect(toastEl!.className).toContain("bg-[#0c0c0e]");
    expect(toastEl!.className).toContain("rounded-lg");
  });
});
