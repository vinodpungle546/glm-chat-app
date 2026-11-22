/** The Quorum logo mark. */
import { clsx } from "@/lib/format";

export const PRODUCT = "Quorum";
export const TAGLINE = "Private meeting intelligence";

/** The logo as an inline SVG. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={clsx("h-6 w-6", className)} aria-hidden>
      <rect width="64" height="64" rx="14" fill="var(--accent)" />
      <circle cx="32" cy="18" r="6" fill="var(--accent-fg)" />
      <circle cx="18" cy="40" r="6" fill="var(--accent-fg)" fillOpacity=".85" />
      <circle cx="46" cy="40" r="6" fill="var(--accent-fg)" fillOpacity=".85" />
      <path d="M22 50c3 3 6.5 4.5 10 4.5S39 53 42 50" stroke="var(--accent-fg)" strokeWidth="3.5"
            strokeLinecap="round" fill="none" />
    </svg>
  );
}
