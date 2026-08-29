import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

describe("DropdownMenu", () => {
  it("opens on click and shows menu items at z-[130]", async () => {
    render(
      <DropdownMenu>
        <DropdownMenuTrigger aria-label="Account menu">Avatar</DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="extra-class">
          <DropdownMenuItem>My videos</DropdownMenuItem>
          <DropdownMenuItem>Sign out</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );

    fireEvent.keyDown(screen.getByRole("button", { name: "Account menu" }), { key: "Enter" });
    const menu = await screen.findByRole("menu");
    expect(menu.className).toContain("z-[130]");
    expect(menu.className).toContain("extra-class");
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });
});
