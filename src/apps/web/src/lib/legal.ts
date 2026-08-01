// Placeholder tokens for the Terms of Service and Privacy Policy pages.
// Every legal-entity fact the pages need lives here, once, so it's trivially
// greppable before publish: `grep -rn "\[YOUR\|\[⚠️" src/apps/web/src/lib/legal.ts`.
//
// Kria is currently operated as a sole proprietorship (no corporate entity
// yet) — see the "Before publish" checklist in the legal-pages PR. Fill in
// every bracketed token below before these pages go live at usekria.com.

// No company formed yet — Kria is operated by the founder as a sole
// proprietorship, so the individual's own legal name is what's used here (a
// DBA filing to make "Kria" the official trade name is a cheap later option,
// not required to publish). Confirm this is the right name before publish.
export const LEGAL_ENTITY = "Emir Erben";
// City/state only, not a full street address — a deliberate early-stage
// choice (see docs/legal/README.md) to keep a home address off a public
// page. Revisit before scaling to EU users, who may expect a fuller DPA-style
// contact address.
export const LEGAL_ADDRESS = "Istanbul, Turkey";
export const GOVERNING_LAW = "[STATE / COUNTRY WHOSE LAW GOVERNS]";

// Single Gmail inbox for all legal contact — no custom-domain email needed
// while pre-entity. Kept as two named exports (rather than one shared
// constant) so the two documents can point at different addresses later
// without a second migration.
export const PRIVACY_EMAIL = "usekria@gmail.com";
export const LEGAL_EMAIL = "usekria@gmail.com";

export const EFFECTIVE_DATE = "August 1, 2026";

// Fixed floor for the liability cap. With no billing in the product today,
// "fees paid in the prior 12 months" evaluates to $0 for every user, which
// several courts treat as no real limitation (or no consideration) at all.
// The Terms use max(fees paid, LIABILITY_CAP_USD).
export const LIABILITY_CAP_USD = 100;

// Notice period for material changes to either document (skill guidance: 30
// days is the enforceability floor for unilateral-modification clauses).
export const CHANGE_NOTICE_DAYS = 30;

// Data-subject / CCPA request response commitment.
export const REQUEST_RESPONSE_DAYS = 30;
