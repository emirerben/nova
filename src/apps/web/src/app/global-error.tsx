"use client";

export default function GlobalError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body style={{ margin: 0, backgroundColor: "#000", color: "#fff" }}>
        <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "0 1rem" }}>
          <h2 style={{ fontSize: "1.25rem", fontWeight: 600, marginBottom: "0.5rem" }}>Something went wrong</h2>
          <p style={{ color: "#a1a1aa", fontSize: "0.875rem", marginBottom: "1.5rem", textAlign: "center", maxWidth: "28rem" }}>
            An unexpected error occurred. Please try again.
          </p>
          <div style={{ display: "flex", gap: "0.75rem" }}>
            <button
              onClick={reset}
              style={{ padding: "0.5rem 1rem", backgroundColor: "#fff", color: "#000", borderRadius: "0.5rem", fontSize: "0.875rem", fontWeight: 500, border: "none", cursor: "pointer" }}
            >
              Try again
            </button>
            <a
              href="/"
              style={{ padding: "0.5rem 1rem", border: "1px solid #3f3f46", borderRadius: "0.5rem", fontSize: "0.875rem", color: "#d4d4d8", textDecoration: "none" }}
            >
              Back to home
            </a>
          </div>
        </div>
      </body>
    </html>
  );
}
