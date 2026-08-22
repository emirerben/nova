import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

describe("Tabs", () => {
  it("renders the stock segmented-pill style and switches panels on click", async () => {
    const user = userEvent.setup();
    render(
      <Tabs defaultValue="visuals">
        <TabsList className="extra-class">
          <TabsTrigger value="visuals">Visuals</TabsTrigger>
          <TabsTrigger value="motion">Motion</TabsTrigger>
        </TabsList>
        <TabsContent value="visuals">Visuals panel</TabsContent>
        <TabsContent value="motion">Motion panel</TabsContent>
      </Tabs>
    );

    const list = screen.getByRole("tablist");
    expect(list.className).toContain("rounded-lg");
    expect(list.className).toContain("bg-muted");
    expect(list.className).not.toContain("border-b");
    expect(list.className).toContain("extra-class");

    expect(screen.getByText("Visuals panel")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Motion" }));
    expect(await screen.findByText("Motion panel")).toBeInTheDocument();
  });
});
