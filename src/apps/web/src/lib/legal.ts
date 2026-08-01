// Placeholder tokens for the Terms of Service and Privacy Policy pages.
// Every legal-entity fact the pages need lives here, once, so it's trivially
// greppable before publish: `grep -rn "\[YOUR\|\[⚠️" src/apps/web/src/lib/legal.ts`.
//
// Kria is currently operated as a sole proprietorship (no corporate entity
// yet) — see the "Before publish" checklist in the legal-pages PR. Fill in
// every bracketed token below before these pages go live at usekria.com.

export const LEGAL_ENTITY = "[YOUR FULL LEGAL NAME]"; // sole proprietor — no company formed yet
export const LEGAL_ADDRESS = "[YOUR REGISTERED / MAILING ADDRESS]";
export const GOVERNING_LAW = "[STATE / COUNTRY WHOSE LAW GOVERNS]";

// hello@usekria.com is already a live Resend sender (app/tasks/email.py). These
// two are NOT yet provisioned as receiving inboxes — create them before publish,
// a policy naming an unmonitored address is worse than naming none.
export const PRIVACY_EMAIL = "privacy@usekria.com";
export const LEGAL_EMAIL = "legal@usekria.com";

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
