import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import CreatorDirectionReceipt from "@/app/plan/_components/CreatorDirectionReceipt";

describe("CreatorDirectionReceipt", () => {
  const receipt = {
    enabled: true,
    applied_count: 3,
    enforced_count: 1,
    advisory_count: 1,
    unsupported_count: 1,
    conflicted_count: 0,
    rules: [
      { normalized_key: "font_family", label: "Use Playfair Display", status: "enforced" as const, scope_label: "Account preference" },
      { normalized_key: "tone", label: "Keep the tone warm", status: "advisory" as const, scope_label: "Account preference" },
      { normalized_key: "captions", label: "Use karaoke captions", status: "unsupported" as const, reason: "This format does not support karaoke captions." },
    ],
  };

  beforeEach(() => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, status: 204, json: async () => ({}) }) as unknown as typeof fetch;
  });

  it("expands into per-rule statuses without making the clean receipt a card", () => {
    render(<CreatorDirectionReceipt receipt={receipt} projectId="thread-1" expectedRevision={4} />);
    fireEvent.click(screen.getByRole("button", { name: /Personalization · 3 applied/ }));

    expect(screen.getByText("Use Playfair Display")).toBeTruthy();
    expect(screen.getByText("Enforced · Account preference")).toBeTruthy();
    expect(screen.getByText("Advisory · Account preference")).toBeTruthy();
    expect(screen.getByText("Not supported")).toBeTruthy();
    expect(screen.getByText("This format does not support karaoke captions.")).toBeTruthy();
  });

  it("explains paused and empty project states", () => {
    const { rerender } = render(
      <CreatorDirectionReceipt
        receipt={{ ...receipt, enabled: false }}
        projectId="thread-1"
      />,
    );
    expect(screen.getByText("Personalization is paused.")).toBeTruthy();

    rerender(
      <CreatorDirectionReceipt
        receipt={{
          ...receipt,
          applied_count: 0,
          enforced_count: 0,
          advisory_count: 0,
          unsupported_count: 0,
          rules: [],
        }}
        projectId="thread-1"
      />,
    );
    expect(screen.getByText("No saved preferences were used for this project.")).toBeTruthy();
  });

  it("saves and clears a project-only override", async () => {
    const overrideReceipt = {
      ...receipt,
      memory_revision: 5,
      rules: [
        { normalized_key: "font_family", label: "Use Inter", status: "enforced" as const, scope_label: "This project only", overridden: true },
        ...receipt.rules.slice(1),
      ],
    };
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ revision: 5, direction_receipt: overrideReceipt }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ revision: 6, direction_receipt: { ...receipt, memory_revision: 6 } }),
      });
    render(<CreatorDirectionReceipt receipt={receipt} projectId="thread-1" expectedRevision={4} />);
    fireEvent.click(screen.getByRole("button", { name: /Personalization · 3 applied/ }));
    fireEvent.click(screen.getAllByRole("button", { name: "Override for this project" })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Project-only preference for Use Playfair Display" }), { target: { value: "Use a lighter serif for this project" } });
    fireEvent.click(screen.getByRole("button", { name: "Save for this project" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      "/api/plan/creation-threads/thread-1/direction-overrides",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(await screen.findByText("Use Inter")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Use account preference" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use account preference" }));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      "/api/plan/creation-threads/thread-1/direction-overrides/font_family",
      expect.objectContaining({ method: "DELETE" }),
    ));
    expect(await screen.findByText("Use Playfair Display")).toBeTruthy();
    expect(JSON.parse((global.fetch as jest.Mock).mock.calls[1][1].body)).toEqual(
      expect.objectContaining({ expected_revision: 5 }),
    );
  });
});
