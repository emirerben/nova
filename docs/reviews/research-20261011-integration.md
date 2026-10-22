# PR1553 historical reference-bank integration

Original c8e5604f76064f2e98c31973dc1c12ba913872c0 is retained by normal merge from fresh mainc97404dbe. These are strategy/style reference inputs, not reliability fixes or evidence that a creator's edit/export was fulfilled. No new fetch, model, video inspection or paid research occurred. Direct Linear searches for1553/1676 found no ticket; related KRI64 defines responsible per-post performance data and limitations. This is a bounded contribution to that data context, not fulfillment or closure of KRI64.

## Existing source recovered and independently calculated

The original11October snapshots are in the shared checkout's ignored research/tiktok/raw directory; the18October snapshots are in the original18October task worktree. They are existing author artifacts, not reconstructed values. Four accounts/20 posts each per date. All80 stored11October view-index/engagement pairs were recomputed and agree with the original deterministic fetch formulas. The sanitized derived audit research-20261011-provenance.json retains source file hashes/timestamps, all sample member IDs/views, medians and numerator counts for the three cited posts; raw bodies and signed media URLs remain outside Git.

- Hike7669218300676607239:12,100views /606.5 sample median =19.9505; (3052+2+2)/12100 =0.252562. Highest engagement rate among these80 sampled posts, not a population or causal result.
- Farm return7668975853270813960:731,300/37,750 =19.3722; (51700+327+7528)/731300 =0.081437.
- Jacket choice7684196677896506642:17,800/4365.5 =4.0774; (2324+21+37)/17800 =0.133820.

## Corrections and union decision

The farm-return metric was incorrectly attached to the routine-reset persona/idea, which instead draws on a disclosed sponsored routine post. Both wrong performance weights are removed; qualitative inspiration remains clearly attributed without an organic-performance claim. True farm/hike/jacket rows carry the exact post URL and observation date/counts. The farm post did not outperform the entire recent catalog: a disclosed sponsored post has10.2M views. Claims that a hook caused comments/saves/engagement are replaced with observed sample comparisons and explicit creative hypotheses. Overlay effects/colors are authored suggestions, not asserted observations from unviewed footage.

All10 original new reference entries remain. All existing main bank objects are semantically unchanged, verified by ID/object comparison:7personas,22ideas,15factors,27overlay examples. The18October PR reuses the same farm/jacket/routine posts under different concept IDs; its deliberate union will keep these earlier canonical IDs and update dated measurements rather than count duplicate posts as new evidence. Distinct illustrative design alternatives may remain labeled as such.

Current creator-language/creator-direction source logic and prompt-template bytes remain intact. Bank tags and all four consuming-agent prompt versions advance to2026-10-22-research-11, not the stale original2026-10-11 prompt baseline. No model/profile change or StageE/F/cache-guard escape marker.

## Verification and limits

92 existing bank/schema/ranking/coupling/overlay-example guards pass. Full free recorded evals:2132 passed,15 skipped,7 known expected failures. Existing consuming-agent tests run in /private/tmp/nova-1553-consuming-agents.log; normal preship and exact-head hosted checks remain required. Replays validate structural contracts, not fresh model preference, organic causality or a new physical export. No paid/live/judge result is claimed or required for these bounded historical-data corrections.

The four consuming-agent suites also passed84 cases. The following offline acceptance command reads the retained primary snapshot and actual bank files; its original-source control must expose both wrongly weighted reset entries before the corrected artifacts pass:

```python
import json, statistics, subprocess
from pathlib import Path
root = Path('/Users/emirerben/Projects/nova/research/tiktok/raw')
records = {}
for path in root.glob('*-20261011.json'):
    source = json.loads(path.read_text())
    median = statistics.median(row['view_count'] for row in source['videos'] if row.get('view_count', 0) > 0)
    for row in source['videos']:
        records[row['id']] = (source, row, median)
banks = {}
for name, key in [('persona_archetypes', 'archetypes'), ('content_ideas', 'ideas')]:
    path = 'src/apps/api/prompts/' + name + '.json'
    current = {row['id']: row for row in json.loads(Path(path).read_text())[key]}
    original = {row['id']: row for row in json.loads(subprocess.check_output(['git', 'show', 'c8e5604f76064f2e98c31973dc1c12ba913872c0:' + path]))[key]}
    banks.update(current)
    wrong = 'wellness-ritual-reset-01' if name == 'persona_archetypes' else 'routine-reset-after-disruption-01'
    assert original[wrong]['performance']['view_index'] == 19.4  # Historical wrong-post control.
    assert current[wrong].get('performance') is None
for concept, post in [('early-riser-commitment-01', '7669218300676607239'), ('early-morning-commitment-01', '7669218300676607239'), ('series-comeback-accountability-01', '7668975853270813960'), ('audience-opinion-fashion-01', '7684196677896506642')]:
    source, row, median = records[post]
    signal = banks[concept]['performance']
    assert signal['view_index'] == round(row['view_count'] / median, 4)
    assert signal['engagement_rate'] == round(sum(row[key] for key in ('like_count', 'comment_count', 'repost_count')) / row['view_count'], 6)
    assert signal['sampled_at'] == source['fetched_at'][:10]
    assert '/video/' + post in banks[concept]['source']
print('PASS: two historical wrong-post controls retained; four current references match independent dated source counts and post identity.')
```

Initial preship correctly blocked all four JSON banks as unmapped journey paths. Added one market-research-reference-banks journey running the existing bank/coupling/slop and four consuming-agent/replay suites whenever those reference inputs change. All prior journey objects and native requirements are unchanged. The original unmapped-path output is the negative control; the exact same four paths now have no mapping gaps. Existing selector tests run unchanged. Receipt validation also caught a diagnostic angle-bracket filename as a placeholder; the command label was corrected without changing its checks.
