import KriaWordmark from "./KriaWordmark";

/**
 * Backwards-compatible alias for the approved Kria wordmark.
 *
 * The former fanned-frame SVG was retired from the web chrome. Keeping this
 * export avoids breaking older imports while ensuring they render the same
 * DynaPuff mark as every current surface.
 */
export default function KriaMark({ className }: { className?: string }) {
  return <KriaWordmark className={className} />;
}
