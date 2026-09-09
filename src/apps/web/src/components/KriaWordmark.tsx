import styles from "./KriaWordmark.module.css";

/**
 * Kria's approved web wordmark.
 *
 * The brand artwork is a hand-lettered DynaPuff treatment rather than a
 * typeset product title. Keeping the letters as separate spans preserves the
 * supplied rhythm at responsive sizes while allowing the current color to be
 * selected by the consuming surface.
 */
export default function KriaWordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`${styles.wordmark} ${className}`} aria-hidden="true">
      <span className={styles.k}>k</span>
      <span className={styles.r}>r</span>
      <span className={styles.i}>i</span>
      <span className={styles.a}>a</span>
    </span>
  );
}
