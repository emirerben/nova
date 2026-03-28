/**
 * Google Identity Services + Google Picker integration.
 *
 * Loads scripts on mount (prevents popup blockers), handles OAuth consent,
 * and renders the Drive file picker filtered to video files.
 */

const GIS_SCRIPT_URL = "https://accounts.google.com/gsi/client";
const PICKER_SCRIPT_URL = "https://apis.google.com/js/api.js";

// Video MIME types the Picker will show
const VIDEO_MIME_TYPES = [
  "video/mp4",
  "video/quicktime",
  "video/x-msvideo",
  "video/webm",
  "video/x-matroska",
].join(",");

export interface DriveFileSelection {
  fileId: string;
  fileName: string;
  mimeType: string;
  sizeBytes: number;
}

// ── Script Loading ──────────────────────────────────────────────────────────

let gisLoaded = false;
let pickerLoaded = false;

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve();
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(script);
  });
}

export async function loadGisScript(): Promise<void> {
  if (gisLoaded) return;
  await loadScript(GIS_SCRIPT_URL);
  gisLoaded = true;
}

export async function loadPickerScript(): Promise<void> {
  if (pickerLoaded) return;
  await loadScript(PICKER_SCRIPT_URL);
  // gapi.load('picker') is required after the script loads
  await new Promise<void>((resolve) => {
    window.gapi.load("picker", { callback: resolve });
  });
  pickerLoaded = true;
}

/** Preload both scripts on component mount. Call early to avoid popup blockers. */
export async function preloadDriveScripts(): Promise<void> {
  await Promise.all([loadGisScript(), loadPickerScript()]);
}

// ── OAuth Token Request ─────────────────────────────────────────────────────

const CONSENT_TIMEOUT_MS = 120_000; // 2 minutes

/**
 * Request a Google OAuth access token with drive.readonly scope.
 * Pops a Google consent window. Resolves with the access token.
 */
export function requestDriveAccessToken(clientId: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const timeoutId = setTimeout(() => {
      reject(new Error("Google sign-in timed out. Please try again."));
    }, CONSENT_TIMEOUT_MS);

    const tokenClient = window.google.accounts.oauth2.initTokenClient({
      client_id: clientId,
      scope: "https://www.googleapis.com/auth/drive.readonly",
      callback: (response: { access_token?: string; error?: string }) => {
        clearTimeout(timeoutId);
        if (response.error) {
          reject(new Error(`Google Drive access was denied: ${response.error}`));
          return;
        }
        if (response.access_token) {
          resolve(response.access_token);
        } else {
          reject(new Error("No access token received from Google."));
        }
      },
      error_callback: (error: { type: string; message?: string }) => {
        clearTimeout(timeoutId);
        if (error.type === "popup_closed") {
          reject(new Error("popup_closed"));
          return;
        }
        reject(new Error(error.message ?? "Google authentication error"));
      },
    });

    tokenClient.requestAccessToken();
  });
}

// ── Google Picker ───────────────────────────────────────────────────────────

/**
 * Open the Google Drive Picker to select video files.
 * Returns selected file(s) metadata. Pass multiSelect=true for template mode.
 */
export function openDrivePicker(
  accessToken: string,
  apiKey: string,
  options: { multiSelect?: boolean } = {},
): Promise<DriveFileSelection[]> {
  return new Promise((resolve, reject) => {
    try {
      const view = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS_VIDEOS)
        .setMimeTypes(VIDEO_MIME_TYPES)
        .setMode(window.google.picker.DocsViewMode.LIST);

      const builder = new window.google.picker.PickerBuilder()
        .addView(view)
        .setOAuthToken(accessToken)
        .setDeveloperKey(apiKey)
        .setTitle("Select video files")
        .setCallback((data: PickerCallbackData) => {
          if (data.action === "cancel" || data.action === window.google.picker.Action.CANCEL) {
            resolve([]);
            return;
          }
          if (data.action === "picked" || data.action === window.google.picker.Action.PICKED) {
            const files: DriveFileSelection[] = (data.docs ?? []).map((doc) => ({
              fileId: doc.id,
              fileName: doc.name,
              mimeType: doc.mimeType,
              sizeBytes: doc.sizeBytes ?? 0,
            }));
            resolve(files);
          }
        });

      if (options.multiSelect) {
        builder.enableFeature(window.google.picker.Feature.MULTISELECT_ENABLED);
      }

      builder.build().setVisible(true);
    } catch (err) {
      reject(err instanceof Error ? err : new Error("Failed to open Google Drive picker"));
    }
  });
}

// ── Type declarations for Google APIs ───────────────────────────────────────

interface PickerCallbackData {
  action: string;
  docs?: Array<{
    id: string;
    name: string;
    mimeType: string;
    sizeBytes?: number;
  }>;
}

declare global {
  interface Window {
    gapi: {
      load: (api: string, config: { callback: () => void }) => void;
    };
    google: {
      accounts: {
        oauth2: {
          initTokenClient: (config: {
            client_id: string;
            scope: string;
            callback: (response: { access_token?: string; error?: string }) => void;
            error_callback?: (error: { type: string; message?: string }) => void;
          }) => { requestAccessToken: () => void };
        };
      };
      picker: {
        DocsView: new (viewId: string) => {
          setMimeTypes: (types: string) => { setMode: (mode: string) => unknown };
        };
        DocsViewMode: { LIST: string };
        PickerBuilder: new () => {
          addView: (view: unknown) => { setOAuthToken: (token: string) => unknown } & Record<string, (...args: unknown[]) => unknown>;
          setOAuthToken: (token: string) => unknown;
          setDeveloperKey: (key: string) => unknown;
          setTitle: (title: string) => unknown;
          setCallback: (cb: (data: PickerCallbackData) => void) => unknown;
          enableFeature: (feature: string) => unknown;
          build: () => { setVisible: (v: boolean) => void };
        };
        ViewId: { DOCS_VIDEOS: string };
        Action: { CANCEL: string; PICKED: string };
        Feature: { MULTISELECT_ENABLED: string };
      };
    };
  }
}
