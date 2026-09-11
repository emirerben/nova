# Automated release metadata

Feature PRs do not edit `VERSION`, `CHANGELOG.md`, or the root package version
fields. Once a PR merges to `main`, the **Release metadata** GitHub workflow
creates one bot commit that owns all four files.

`VERSION` is the canonical four-part release number. The root `package.json`
and `package-lock.json` receive its first three components.

## Labels

The default is a patch release under **Changed**. Add at most one bump label and
at most one category label when the default is not right:

- Bump: `release:major`, `release:minor`
- Category: `release:added`, `release:fixed`, `release:changed`,
  `release:deprecated`, `release:removed`, `release:security`

Missing or conflicting labels intentionally fall back to patch + Changed. The
workflow uses the PR title and number verbatim as the changelog entry; it does
not generate or infer release copy.

## Recovery

The release workflow is idempotent per PR, recorded with an internal changelog
marker. It retries an optimistic push race five times against fresh `main`.
If it still fails, correct the reported repository/permission problem and
re-run the workflow. Do not manually patch release files in a feature PR.

## GitHub App setup

The workflow authenticates as a dedicated GitHub App, never as a contributor.
Create an app named `nova-release-metadata` with **Contents: Read and write**
repository permission, no webhooks, and install it
only on `emirerben/nova`. Add the app to a `main` branch ruleset bypass list;
keep the existing pull-request approval, CODEOWNERS, and required-check rules
in force for every other actor.

Store the generated app ID and private key as repository Action secrets named
`RELEASE_METADATA_APP_ID` and `RELEASE_METADATA_APP_PRIVATE_KEY` respectively.
The workflow mints a short-lived installation token for each run. Rotate the
private key by adding a replacement secret before deleting the old key.
