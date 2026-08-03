/**
 * Download a rendered video to the user's device.
 *
 * Hands the signed attachment URL straight to the browser. This is deliberately
 * not a fetch→Blob flow: rendered MP4s can be hundreds of MB, and retaining the
 * complete file in a mobile tab caused long delays and memory pressure before the
 * OS download UI even started. Legacy inline URLs open in a new tab, where the
 * browser can still stream them without a JavaScript-sized buffer.
 */
export function downloadVideo(
  url: string,
  filename: string,
  openLegacyInNewTab = false,
): Promise<void> {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  if (openLegacyInNewTab) a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  return Promise.resolve();
}
