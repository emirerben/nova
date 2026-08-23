"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { getPersona, updatePersona } from "@/lib/plan-api";
import type { PersonaContent, PersonaResponse } from "@/lib/plan-api";
import PersonaEditor from "../_components/PersonaEditor";
import { LightShell } from "../_components/ui/LightShell";
import { Button } from "@/components/ui/button";

export default function PersonaPage() {
  const { status } = useSession();
  const router = useRouter();
  const [persona, setPersona] = useState<PersonaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);

  useEffect(() => {
    if (status === "unauthenticated") {
      router.replace("/plan");
      return;
    }
    if (status !== "authenticated") return;
    setLoading(true);
    setLoadError(false);
    getPersona()
      .then((p) => {
        if (!p || p.persona_status === "chat_pending" || !p.persona) {
          router.replace("/plan");
          return;
        }
        setPersona(p);
      })
      .catch(() => setLoadError(true))
      .finally(() => setLoading(false));
  }, [status, router, loadAttempt]);

  if (loading) {
    return (
      <LightShell size="narrow">
        <p role="status" aria-live="polite" className="py-12 text-sm text-[#71717a]">
          Loading your creator profile…
        </p>
      </LightShell>
    );
  }

  if (loadError || !persona || !persona.persona) {
    return (
      <LightShell size="narrow">
        <div role="alert" className="py-12">
          <h1 className="font-display text-3xl text-[#0c0c0e]">
            Your creator profile couldn&apos;t load
          </h1>
          <p className="mt-3 text-sm text-[#71717a]">
            Check your connection and try again. Your saved profile is safe.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Button type="button" variant="ink" onClick={() => setLoadAttempt((n) => n + 1)}>
              Retry loading profile
            </Button>
            <Button asChild type="button" variant="outline">
              <Link href="/plan">Back to content plan</Link>
            </Button>
          </div>
        </div>
      </LightShell>
    );
  }

  async function handleSaved(updated: PersonaContent) {
    if (!persona) return;
    const refreshed = await updatePersona(persona.id, updated);
    setPersona(refreshed);
  }

  return (
    <LightShell size="narrow">
      <PersonaEditor
        persona={persona.persona}
        status={persona.persona_status}
        onSave={handleSaved}
        onContinue={() => router.push("/plan")}
        continueLabel="Back to content plan"
        tiktokProfile={persona.tiktok_profile}
      />
    </LightShell>
  );
}
