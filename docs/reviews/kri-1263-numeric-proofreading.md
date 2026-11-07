# KRI-1263: numeric proofreading guard

## Acceptance and scope

Follow-up to the numeric-corruption finding on
[PR #2157](https://github.com/emirerben/nova/pull/2157#pullrequestreview-5811032670).
The synthetic incident is `it cost 1.5 dollars` with the proposed correction
`it cost 15 dollars`. The required output is the original text and timed words.
No customer output or media was accessed for this replay.

For a timed cue, proofreading must preserve every numeric-bearing token exactly.
Decimal marks, signs, separators, exponents, fractions, times, currency, percent
signs, accounting parentheses, superscript digits, Unicode fractions and numerals
can carry meaning. Compatibility forms that produce numeric characters under
NFKD (such as `㎟` to `mm2`) are numeric too. This
guard does not infer a number's locale or which punctuation is cosmetic. Thus a
proposed `1.5` to `1.5,` also keeps `1.5`. Ordinary spelling and case/punctuation
corrections elsewhere in the cue must still land, with the existing rule that
punctuation immediately beside a refused replacement stays spoken.

Independent literal expectations, recorded in the regression matrix before the
implementation change:

- `1.5` must not become `15`, and `15` must not become `1.5`.
- `.5`, `1,5`, `1,000`, and `1.000,5` must keep every separator.
- `-5`, `−5`, `+5`, and `1e-5` must keep the sign.
- `1/2`, `1:30`, `5%`, `$5`, `(5)`, and `10²` must keep their full notation.
- `1.5,` must not become `15.`; attached sentence punctuation is conservative too.
- `well it cost -1.5 dolars again today` proposed as
  `Well, it cost 15 dollars again today.` must become
  `Well, it cost -1.5 dollars again today.`.
- `well it cost 1.5 dollars today` may become
  `Well, it cost 1.5 dollars today.`. The numeric token did not change.
- Review regression: `14` to `¼`, `12` to `½`, `13` to `⅓`, and reverse
  `¼` to `14` must keep the original, even though these fractions are not digits.
- `Ⅳ` to `Ⅵ`, `x四` to `x五`, and `$¼` to `$½` must be refused as numeric edits.
- `㎟` to/from `mm2`, `㋀` to `1月`, `㍙` to `1点`, `1日` to `㏠`, and `⼀` to `一`
  must not bypass the guard through normalization.
- Split/merge proposals containing `½`, `Ⅳ` or `㎟` must keep the original words
  and timing. Unchanged Unicode numeric tokens still allow surrounding punctuation.

## Boundary and cause

The public `correct_caption_cues(..., trusted_aliases=None)` entry point calls
the freeform proofreader, parses its lines and retimes accepted text onto the
original timed words. `_word_key` previously folded away punctuation before
alignment. `1.5` and `15` therefore shared the key `15`, and the equal opcode
accepted the replacement without the digit rejection in `_is_word_edit`.

Numeric keys now retain the original token before folding. One shared predicate
checks raw Unicode numeric characters and numeric characters introduced by NFKD.
The same predicate guards replacement and respacing, so preserving the raw token
cannot bypass those checks. The alias path, untimed-cue legacy behavior, provider
prompt, settings, word timing and rendering code are unchanged.

## Executed evidence

Base: `24a4c8b10bdb218af053e271d999f8132bd12dec`. Final tested revision is bound
in the PR's request-evidence record.

From `src/apps/api`:

```sh
.venv/bin/python -m pytest tests/pipeline/test_caption_correct.py -q
.venv/bin/python -m pytest tests/pipeline/test_caption_correct.py tests/pipeline/test_caption_spelling.py tests/pipeline/test_smart_caption_prepare.py tests/pipeline/test_caption_language.py tests/tasks/test_kri793_talking_caption_spellings.py tests/tasks/test_green45_talking_caption_text.py -q
```

- Before the fix: the new numeric matrix has 19 failures and one passing control.
- The initial digit-only fix passed 54 correction tests and 253 neighboring tests,
  but independent review found Unicode numeric bypasses. That evidence did not
  establish the broader numeric safety claim.
- After the Unicode correction: the full correction suite has 82 passes.
- Six-file neighboring suite: 281 passes, no failures or skips.
- Red control: temporarily restoring only `caption_correct.py` from the base
  makes the final numeric matrix fail 19/20 again; the independent allowed
  punctuation control passes. The fixed source was restored immediately.
- Unicode red control: restoring the first fix at `24507bc8` makes the expanded
  numeric matrix fail 15 cases, with 33 controls passing. A key-only mutation that
  leaves digit-only replacement/respacing checks fails 10 cases, proving those
  acceptance paths also need the shared numeric predicate.
- A separate fake OpenAI SDK transport replay exercised the real public entry
  point, prompt assembly, response parser and retimer. The decimal incident is
  refused, independent spelling/punctuation lands, and original word IDs survive.
  One synthetic provider response, zero external calls.
- The existing `kri-708-caption-correction` CI journey runs the entire correction test
  file, including these new cases.

## Remaining gaps

This is an offline local fix. No paid model call, real-device export, production
change, flag change or native-output verification was performed. KRI-1263 remains
the owner of original-request fulfillment, export and rollout verification.

The adjacent split/spelling issue from #2157 is separate and still reproduced:
`I have alot fo time` proposed as `I have a lot of time`, and `your alot better`
proposed as `you're a lot better`, each retain the original text. Solving that
requires different local alignment and is intentionally outside this safety fix.

Untimed cues retain their existing unrestricted legacy correction behavior. The
numeric safety claim here is limited to timed cues on the affected iPhone backend
path. Independent provider transcription accuracy and number words without digits
are not established by these tests.
