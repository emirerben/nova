# Aligned source-window control (KRI-831)

An independently encoded moving source cut starting exactly at1.5s passed when every sampled picture was one frame late: all7 checks matched only the next source frame. The pre-code E2E control failed semantically (1failed/3passed), establishing a false acceptance rather than a tool exception.

The correction rejects an all-neighbour window only when both the source start and all observed sample instants align with real source frame timestamps. Isolated neighbour matches, off-grid cuts, held frames and differently resampled proxy clocks remain supported. A second pre-code positive exposed an over-broad first correction: a correct30fps original checked against its29.97fps proxy needs neighbour matches legitimately. That positive failed the first guard and passed the original implementation on identical media; the final guard retains it. No pixel/SSIM/OCR/audio threshold, time budget, sample count or publication identity fence changed.

## Verification

- Current final verifier module:50 passed in58.30s; the sole other case could not bind its localhost HTTP test server under the filesystem sandbox. That unchanged security control then passed with the required local socket permission (0.81s). All51 cases passed across these two runs, not one literal51-pass run. Earlier disk-full attempts remain failed environmental runs, not passing evidence.
- Existing authenticated actual-media completion and rejected-output publication/retry controls:21 passed. Storage/database transport are local doubles; MP4 decoding, request/source resolution and completion/publication handlers are real.
- Both retained Troya C2 and C3 outputs passed against the exact captured status, recipe digest and independently inventory-hashed proxies after the final correction. Their montages were inspected. Each retains one legitimate neighbour sample in the first clip; the deliberate all-neighbour aligned shift is rejected.
- Original46 verifier cases and all existing completion/publication assertions remain. Five E2E timing controls were added before the corresponding correction/refinement. The supported resampled-proxy positive is retained, not waived.

See kri-831-retained-replay.json for output/report hashes and scoped results. Private media, source maps, failed controls, decoded fixtures and logs remain local; no creator footage is committed. Commands use the existing tests/integration/test_actual_export_verification.py, tests/integration/test_actual_export_completion.py and tests/routes/test_actual_export_publication.py; retained local media is checked with python -m app.cli.verify_device_export and explicit --status, --sources, --video, --brand-tail standard, --report and --montage paths.

## Limits

This is a bounded verifier correction, not general one-frame discrimination across different sample clocks. It does not establish physical iPhone Save/reopen/Photos fulfillment (KRI-830). Verifier-v3 forward-only rollout and rollback compatibility remain KRI-832; no deployment convergence or old-server compatibility is claimed here. No phone, model/provider request, production mutation or manual deployment ran.
