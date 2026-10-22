# PR1676 dated canonical reference update

Original cd8e192ae97834ecf25c905bb3f0a3c831e62323 and corrected PR1553 parentfe63b7751 are preserved by normal merges from fresh mainc97404dbe. Merge actual #1553 first. No fresh research/fetch, provider, paid evaluation or video inspection. KRI64 is related responsible-performance-data context; this bounded reference-data contribution does not fulfill or close its broader product work.

Recovered original18October raw metadata in the author's untouched nova-tiktok-research-20261018 worktree:four accounts,20posts each. Recomputed all80 stored view-index/engagement pairs exactly. The sanitized derived research-20261018-provenance.json retains original file hashes/timestamps, sample IDs/views, medians, measured numerator counts and the canonical-alias decisions. Raw bodies and signed media URLs stay outside Git.

## Updated observations are not independent new successes

The farm, hike and jacket IDs are the SAME posts in both weekly snapshots:

- Farm7668975853270813960:733,400views /38,300 sample median =19.1488; engagement0.081364.
- Hike7669218300676607239:12,100/612.5 =19.7551; engagement0.252727.
- Jacket7684196677896506642:19,100/3,567 =5.3546; engagement0.135916.

The different11/18October ratios reflect observed counts and sample denominators, not a new independent post or a measured causal hook effect. Farm-return performance remains absent from the unrelated routine-reset persona/idea. The routine caption is disclosed sponsored content, used as qualitative inspiration only.

## Deliberate duplicate-free union

Keep the earlier canonical IDs for five equivalent concepts rather than weighting the same posts twice:wellness-ritual-lifestyle→wellness-ritual-reset, nostalgia-series-return→series-comeback-accountability, outfit-choice-question→audience-opinion-fashion, ritual-anchor-reset→routine-reset-after-disruption, and nostalgia-content-return-corpus→series-comeback-nostalgia-corpus. Exact full ID mapping is in the audit JSON. Original proposed IDs remain in preserved author history; they were never shipped.

Refresh the canonical measured references to their actual18October snapshots. Retain three distinct additions from the original eight:one choice-question factor and two illustrative overlay design alternatives. The factor describes an invitation to respond, not a proven comment lift or matched-length experiment. Overlay effect/color/footage claims are explicitly unverified authored treatments; the routine overlay wording is concrete and does not teach the banned generic transformation frame.

Combined with1553:2new personas,4new ideas,3new factors and4new overlay examples. All preexisting main objects remain unchanged by ID/object comparison:7/22/15/27 respectively. All canonical1553 concept IDs remain. Four bank/consuming-agent versions advance together to2026-10-22-research-18; creator-language/direction source and prompt-template bytes remain intact. Existing additive bank journey and every prior native requirement remain.

## Checks and remaining scope

178 existing bank/schema/ranking/coupling/slop/consuming-agent cases pass. Full free recorded evals:2132 passed,15 skipped,7 known expected failures. No fresh model preference or actual generated video is implied. Source data and duplicate decisions were defined before edits in the shared research-review-decisions document retained in the1553 task; the initial attempted copy here found no .dev directory, then the existing original plan was copied after creating it. No new failure criteria were inferred from implementation.

Preship and exact final-source CI remain required. Keep this private until actual1553 merge so its squash/version overlap is reconciled once before publication. The historical11October report's acceptance command is scoped to1553sourcefe63; this branch deliberately updates those four signals. Use the18October command below on this branch.

```python
import json, statistics
from pathlib import Path
root = Path('/Users/emirerben/Projects/nova-tiktok-research-20261018/research/tiktok/raw')
records = {}
for path in root.glob('*-20261018.json'):
    source = json.loads(path.read_text())
    median = statistics.median(row['view_count'] for row in source['videos'] if row.get('view_count', 0) > 0)
    for row in source['videos']:
        records[row['id']] = (source, row, median)
banks = {}
for name, key in [('persona_archetypes', 'archetypes'), ('content_ideas', 'ideas'), ('tiktok_success_factors', 'factors'), ('overlay_examples', 'examples')]:
    rows = json.loads(Path('src/apps/api/prompts/' + name + '.json').read_text())[key]
    assert len({row['id'] for row in rows}) == len(rows)
    banks.update({row['id']: row for row in rows})
audit = json.loads(Path('docs/reviews/research-20261018-provenance.json').read_text())
for alias, canonical in audit['canonical_aliases'].items():
    assert alias not in banks and canonical in banks
for unmeasured in ('wellness-ritual-reset-01', 'routine-reset-after-disruption-01'):
    assert banks[unmeasured].get('performance') is None
for concept, post in [('early-riser-commitment-01', '7669218300676607239'), ('early-morning-commitment-01', '7669218300676607239'), ('series-comeback-accountability-01', '7668975853270813960'), ('audience-opinion-fashion-01', '7684196677896506642')]:
    source, row, median = records[post]
    signal = banks[concept]['performance']
    assert signal['view_index'] == round(row['view_count'] / median, 4)
    assert signal['engagement_rate'] == round(sum(row[key] for key in ('like_count', 'comment_count', 'repost_count')) / row['view_count'], 6)
    assert signal['sampled_at'] == source['fetched_at'][:10]
    assert '/video/' + post in banks[concept]['source']
print('PASS: five duplicate-source aliases use canonical IDs; wrong-post weights remain absent; four current signals match independent18October source counts/identity.')
```
