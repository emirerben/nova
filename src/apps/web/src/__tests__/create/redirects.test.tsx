import { redirect } from "next/navigation";
import CreateRedirect from "@/app/create/page";
import ManualCreateRedirect from "@/app/create/manual/page";
import NewVideoRedirect from "@/app/plan/new/page";
import LibraryRedirect from "@/app/library/page";
import GenerativeRedirect from "@/app/generative/page";

jest.mock("next/navigation", () => ({ redirect: jest.fn() }));

describe("legacy creation entry points", () => {
  beforeEach(() => jest.mocked(redirect).mockReset());

  it.each([CreateRedirect, ManualCreateRedirect, NewVideoRedirect, GenerativeRedirect])("redirects old creation UI to chat", (Page) => {
    Page();
    expect(redirect).toHaveBeenCalledWith("/plan");
  });

  it("opens legacy library bookmarks in the chat gallery", () => {
    LibraryRedirect();
    expect(redirect).toHaveBeenCalledWith("/plan?view=gallery");
  });
});
