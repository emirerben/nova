# Local sign-in (dev-login)

Moved out of `CLAUDE.md` (size budget) — this is setup detail, not an invariant.

The content-plan / generative flows are Google-gated. To sign in on localhost
without Google consent, the repo ships a `dev-login` NextAuth provider
(`src/apps/web/src/lib/auth.ts`) gated behind `ALLOW_DEV_LOGIN=true`.

## Setup (once per machine)

Because `dev-auto.sh` sources the repo-root `.env`, setting these there (see
`.env.example`) makes dev-login work in **every worktree** automatically — no
per-worktree setup:

```bash
ALLOW_DEV_LOGIN=true
INTERNAL_API_KEY=<any-string>
```

## Signing in

Go to `http://localhost:3000/api/auth/signin` → "Dev login (local only)" → any
email.

## Gotchas

- Both the API and web must read the **same** `INTERNAL_API_KEY` (sourcing the
  root `.env` guarantees this). The API fail-closes — 401 on plan routes — if
  it is unset.
- **NEVER set `ALLOW_DEV_LOGIN` in Vercel/Fly.**
- If you run web/API by hand (not via `dev-auto.sh`), Next.js loads env from
  `src/apps/web/`, not the repo root — `source ../../../.env` into the launch
  shell.
