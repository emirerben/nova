# Billing gap

The native source inventory has no StoreKit dependency, product identifiers,
purchase sheet, paywall or subscription-management route. Search `src/apps/ios/`
for `StoreKit`, `paywall`, `billing`, `purchase` and `subscription`; distinguish
Combine subscriptions and original-media upgrades from purchases.

The attempted route is sign-in, workspace and Manage account. The account drive
exercises those controls but cannot open a purchase flow that is not implemented.
Run `drive billing` after launch to retain a `not_implemented` gap artifact.
It exits 3 and cannot count as a passing test.

Purchase verification needs a native billing flow, configured store products and
a sandbox account. This task does not add monetization or make a purchase. An
account settings fixture or backend quota check cannot prove payment behavior.
