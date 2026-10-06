# KRI-459 failure modes and verification

Before implementation, the inspected dispatch path reloads the latest brief after
approval, and the montage worker reloads live clip assignments. A later chat or
clip replacement can therefore change the input to already accepted work.

Regression checks must prove:
- An approved request is a deep snapshot, with a digest that rejects corruption.
- The snapshot includes media identities/versions and survives writer-flag rollback.
- Draft saves cannot replace server-owned request authority.
- Pending unbound work needs a new plan; accepted legacy work uses saved inputs.
- Clip replacement before dispatch invalidates the draft; analysis completion alone
  does not invalidate the same media version.
- Receipt evidence is tied to request/output versions. Unknown checks remain visible
  and cannot authorize a success claim or an unapproved simplification.

Unit and database checks are not rendered-media evidence. Live-model budgets,
rendered exports and physical iPhone checks are separately reported.
