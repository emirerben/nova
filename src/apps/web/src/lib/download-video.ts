/**
 * Download a rendered video to the user's device.
 *
 * Fetches the output URL into a blob, triggers an `<a download>` so the browser
 * saves it with `filename` instead of navigating, then revokes the object URL.
 * Falls back to opening the URL in a new tab if the fetch fails (e.g. CORS or an
 * expired signed URL) so the user can still grab it manually.
 */
export async function downloadVideo(url: string, filename: string): Promise<void> {
  try {
    const res = await fetch(url);
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(objectUrl);
  } catch {
    window.open(url, "_blank");
  }
}
