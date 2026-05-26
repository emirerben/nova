import Link from "next/link";
import fs from "node:fs/promises";
import path from "node:path";

// Server component. No data fetching, no client interactivity. The zip is
// served as a sibling static asset under public/admin/extension/, and the
// middleware BasicAuth gate covers this whole subtree via the /admin/:path*
// matcher.

interface ExtensionInfo {
  version: string;
  built_at: string;
  source_commit: string;
}

async function loadExtensionInfo(): Promise<ExtensionInfo | null> {
  const filePath = path.join(
    process.cwd(),
    "public",
    "admin",
    "extension",
    "extension-info.json",
  );
  try {
    const raw = await fs.readFile(filePath, "utf-8");
    const parsed = JSON.parse(raw) as Partial<ExtensionInfo>;
    if (parsed.version && parsed.built_at && parsed.source_commit) {
      return parsed as ExtensionInfo;
    }
    return null;
  } catch {
    return null;
  }
}

export default async function InstallExtensionPage() {
  const info = await loadExtensionInfo();
  const shortCommit = info ? info.source_commit.slice(0, 7) : null;
  const builtAtFormatted = info
    ? new Date(info.built_at).toUTCString().replace("GMT", "UTC")
    : null;

  return (
    <main className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="max-w-3xl mx-auto px-6 py-10">
        <Link
          href="/admin/music"
          className="text-zinc-400 hover:text-zinc-200 text-sm"
        >
          ← Music Tracks
        </Link>

        <h1 className="text-3xl font-bold mt-4 mb-6">
          Install the Nova extension
        </h1>

        <div className="bg-amber-950/60 border border-amber-800/60 text-amber-200 rounded-lg p-4 mb-4 text-sm">
          <strong className="block mb-1">Interim internal install.</strong>
          This page exists so the small set of current admins can install the
          extension without cloning the repo. A future managed/internal
          extension distribution with auto-updates is tracked separately;
          distribution mechanism still to be verified.
        </div>

        <p className="text-xs text-zinc-500 mb-8">
          This page, the linked zip, and <code>/admin/*</code> are protected by
          a temporary admin BasicAuth gate. The gate is a stopgap until a
          proper user-auth system replaces it. The zip contains the
          extension&rsquo;s built bundle only &mdash; no secrets, tokens, or
          service-account keys.
        </p>

        <p className="text-sm text-zinc-400 mb-8">
          <strong>Browser support:</strong> tested on Chrome. Edge and Brave
          are expected to work because they share Chromium MV3, but are not yet
          verified for this interim install path.
        </p>

        {/* Primary CTA */}
        <section className="bg-zinc-900 border border-zinc-800 rounded-lg p-6 mb-8">
          <a
            href="/admin/extension/nova-extension.zip"
            download="nova-extension.zip"
            className="inline-block bg-emerald-600 hover:bg-emerald-500 text-white text-base font-semibold px-6 py-3 rounded-lg transition-colors"
          >
            Download nova-extension.zip
          </a>
          {info ? (
            <p className="text-xs text-zinc-500 mt-3 font-mono">
              Version {info.version} &middot; Built {builtAtFormatted} &middot;
              Source commit {shortCommit}
            </p>
          ) : (
            <p className="text-xs text-amber-400 mt-3">
              Build metadata missing &mdash; run{" "}
              <code>./scripts/build-admin-extension-zip.sh</code> to regenerate
              the zip and <code>extension-info.json</code>.
            </p>
          )}
        </section>

        {/* Install steps */}
        <section className="mb-10">
          <h2 className="text-xl font-semibold mb-4">Install</h2>
          <ol className="space-y-4 text-sm text-zinc-200 list-decimal list-inside">
            <li>
              <strong>Download the zip and unzip it first.</strong> You&rsquo;ll
              get a folder containing <code>manifest.json</code> (plus{" "}
              <code>background.js</code>, <code>offscreen.html</code>, etc.). If
              your unzipper creates a nested folder, the one you want is
              whichever folder directly contains <code>manifest.json</code>.
            </li>
            <li>
              Open <code>chrome://extensions</code>. Enable{" "}
              <strong>Developer mode</strong> using the toggle in the top-right.
            </li>
            <li>
              Click <strong>Load unpacked</strong> and select the{" "}
              <em>unzipped folder</em> (the one containing{" "}
              <code>manifest.json</code>), <em>not</em> the <code>.zip</code>{" "}
              file itself.
            </li>
            <li>
              <strong>Pin the extension</strong>: click the puzzle-piece icon in
              your browser toolbar, then pin &ldquo;Nova Music Ingest&rdquo; so
              it stays visible.
            </li>
            <li>
              <strong>Set admin credentials</strong>: click the Nova extension
              icon to open its popup. Enter the same Nova admin username and
              password you use for the Nova admin pages (the ones the browser
              prompted you for). Click <strong>Test connection</strong>{" "}
              &mdash; it should say <span className="text-emerald-400">OK</span>.
              The extension uses these to talk to <code>/api/admin/*</code> from
              its background context.
            </li>
          </ol>
        </section>

        {/* Updating */}
        <section className="mb-10">
          <h2 className="text-xl font-semibold mb-2">Updating</h2>
          <p className="text-sm text-zinc-300">
            When a new zip is published here: re-download, unzip over the same
            local folder (overwrite), then open <code>chrome://extensions</code>{" "}
            and click <strong>Reload</strong> under Nova Music Ingest.
          </p>
        </section>

        {/* Verification */}
        <section className="mb-10">
          <h2 className="text-xl font-semibold mb-2">Verify it worked</h2>
          <p className="text-sm text-zinc-300">
            Refresh <Link href="/admin/music" className="underline text-emerald-400">/admin/music</Link>.
            The &ldquo;Install Nova extension&rdquo; link should be replaced by
            an enabled <strong>Ingest via extension</strong> button.
          </p>
        </section>

        {/* Troubleshooting */}
        <section className="mb-10">
          <h2 className="text-xl font-semibold mb-3">Troubleshooting</h2>
          <div className="space-y-2 text-sm">
            <details className="bg-zinc-900 border border-zinc-800 rounded p-3">
              <summary className="cursor-pointer text-zinc-200">
                &ldquo;Ingest via extension&rdquo; still disabled after install
              </summary>
              <p className="text-zinc-400 mt-2">
                Hard-refresh <code>/admin/music</code> (
                <kbd>Cmd-Shift-R</kbd> / <kbd>Ctrl-Shift-R</kbd>). If it
                persists, confirm <code>chrome://extensions</code> shows Nova
                Music Ingest as <em>Enabled</em> and the page origin matches the
                manifest&rsquo;s <code>externally_connectable.matches</code>{" "}
                (production <code>nova-video.vercel.app</code> or local{" "}
                <code>localhost:3000</code>).
              </p>
            </details>

            <details className="bg-zinc-900 border border-zinc-800 rounded p-3">
              <summary className="cursor-pointer text-zinc-200">
                &ldquo;Load unpacked&rdquo; button missing
              </summary>
              <p className="text-zinc-400 mt-2">
                The Developer-mode toggle in the top-right of{" "}
                <code>chrome://extensions</code> is off. Flip it on.
              </p>
            </details>

            <details className="bg-zinc-900 border border-zinc-800 rounded p-3">
              <summary className="cursor-pointer text-zinc-200">
                &ldquo;Test connection&rdquo; in the popup returns 401
              </summary>
              <p className="text-zinc-400 mt-2">
                The admin credentials don&rsquo;t match. Re-enter the same
                username and password you use for the browser BasicAuth prompt
                on <code>/admin/*</code> pages.
              </p>
            </details>

            <details className="bg-zinc-900 border border-zinc-800 rounded p-3">
              <summary className="cursor-pointer text-zinc-200">
                &ldquo;Test connection&rdquo; says &ldquo;origin not
                allowlisted&rdquo;
              </summary>
              <p className="text-zinc-400 mt-2">
                The &ldquo;Nova API origin&rdquo; field in the popup must be{" "}
                <code>https://nova-video.vercel.app</code> for production or{" "}
                <code>http://localhost:3000</code> for local dev. Any other
                value is rejected without a network request &mdash; defense in
                depth so the extension can&rsquo;t leak admin credentials to a
                mistyped or tampered origin.
              </p>
            </details>

            <details className="bg-zinc-900 border border-zinc-800 rounded p-3">
              <summary className="cursor-pointer text-zinc-200">
                You selected the .zip and got an error
              </summary>
              <p className="text-zinc-400 mt-2">
                Chrome cannot load a <code>.zip</code> directly. Unzip first;
                then select the folder that contains <code>manifest.json</code>.
              </p>
            </details>

            <details className="bg-zinc-900 border border-zinc-800 rounded p-3">
              <summary className="cursor-pointer text-zinc-200">
                Extension version on <code>chrome://extensions</code> doesn&rsquo;t
                match this page
              </summary>
              <p className="text-zinc-400 mt-2">
                Re-download from this page (always the latest committed build)
                and click <strong>Reload</strong> on the extensions page.
              </p>
            </details>
          </div>
        </section>
      </div>
    </main>
  );
}
