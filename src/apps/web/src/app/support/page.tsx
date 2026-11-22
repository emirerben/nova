import Link from "next/link";

export const metadata = { title: "iPhone Support — Kria" };

export default function SupportPage() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="text-3xl font-semibold">Kria iPhone support</h1>
      <p className="mt-6">For help with sign-in, editing, exports, or your account, email the Kria team.</p>
      <a className="mt-4 inline-block underline" href="mailto:usekria@gmail.com">usekria@gmail.com</a>
      <p className="mt-6">Include your iOS version and the step you need help with.</p>
      <Link className="mt-8 inline-block underline" href="/privacy">Privacy policy</Link>
    </main>
  );
}
