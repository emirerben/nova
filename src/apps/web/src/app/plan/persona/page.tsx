"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { getPersona, updatePersona } from "@/lib/plan-api";
import type { PersonaContent, PersonaResponse } from "@/lib/plan-api";
import PersonaEditor from "../_components/PersonaEditor";
import { LightShell } from "../_components/ui/LightShell";

export default function PersonaPage() {
  const { status } = useSession();
  const router = useRouter();
  const [persona, setPersona] = useState<PersonaResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (status === "unauthenticated") {
      router.replace("/plan");
      return;
    }
    if (status !== "authenticated") return;
    getPersona()
      .then((p) => {
        if (!p || p.persona_status === "chat_pending" || !p.persona) {
          router.replace("/plan");
          return;
        }
        setPersona(p);
      })
      .catch(() => router.replace("/plan"))
      .finally(() => setLoading(false));
  }, [status, router]);

  if (loading || !persona || !persona.persona) return null;

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
        continueLabel="Back to plan →"
        tiktokProfile={persona.tiktok_profile}
        signatureQuote={persona.persona.signature_quote}
      />
    </LightShell>
  );
}
