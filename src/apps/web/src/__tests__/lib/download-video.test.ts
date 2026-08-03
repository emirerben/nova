import { downloadVideo } from "@/lib/download-video";

describe("downloadVideo", () => {
  it("hands the URL directly to the browser without buffering an MP4 in JavaScript", async () => {
    const fetchSpy = jest.fn();
    Object.defineProperty(global, "fetch", { value: fetchSpy, configurable: true });
    let clickedAnchor: HTMLAnchorElement | null = null;
    const clickSpy = jest
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        clickedAnchor = this;
      });

    await downloadVideo("https://storage.example/attachment", "kria-video.mp4");

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const anchor = clickedAnchor as HTMLAnchorElement | null;
    expect(anchor).not.toBeNull();
    if (!anchor) throw new Error("Expected the download anchor to be clicked");
    expect(anchor.href).toBe("https://storage.example/attachment");
    expect(anchor.download).toBe("kria-video.mp4");
    expect(anchor.target).toBe("");
    delete (global as { fetch?: typeof fetch }).fetch;
  });

  it("opens only a legacy inline URL in a new tab", async () => {
    let clickedAnchor: HTMLAnchorElement | null = null;
    jest
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        clickedAnchor = this;
      });

    await downloadVideo("https://storage.example/legacy-inline", "kria.mp4", true);

    const anchor = clickedAnchor as HTMLAnchorElement | null;
    expect(anchor?.target).toBe("_blank");
  });
});
