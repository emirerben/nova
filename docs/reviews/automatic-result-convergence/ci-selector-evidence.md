# UI inventory registration follow-up

Parent source72c248d5ccd8600f93f8f6fa6abe1666dba688a8 retains the reviewed59 PostgreSQL/125 offline correction. This follow-up changes only UI test registration and this evidence; no feature/backend/native implementation or test body changed.

Hosted draft2137 selector job failed161-test contracts with four assertions: focused creation, focused method, helper and header selection fell back to full. Job: https://github.com/emirerben/nova/actions/runs/42256932938/job/128026484819. Local three-method reproduction produced the same four failures. Inventory comparison identified exactly one unclassified method and zero missing methods: KriaUITests/SlidePostUITests/testAIRecoveryRefusesACompletedReceiptForAnotherRequest, added by this feature but omitted from scripts/ios/ui-test-groups.json.

The one-line correction registers it in the existing creation group beside the related failed-AI-recovery case. It does not weaken the inventory guard, selector logic or expected executed coverage. Independent read-only review approved that exact mapping.

Executed from repository root:

```sh
python3 -m unittest discover -s scripts/ci -p 'test*.py'
```

161 tests passed in41.717s, exit0; `/tmp/nova-auto-ci-selection-contracts.log`. `ui_tests.inventory_matches()` now returns true, diffcheck passes. No native/package/model/simulator work ran; the Python suite includes bounded readiness timing controls. Runtime hashes and the59/125 component receipts remain those of72c248d5; this metadata-only follow-up does not reattribute them as a rerun. Native723 app/39package/6UI are still unexecuted. The new hosted run and current-main integration remain pending; skipped descendants of the failed selector are not passed tests.
