# PDF teach-once on a second statement: measured evidence

Card A2 of the 0.5 epic. Base: the approved `c/pdf-land` head (`43de84c`), PDF import step v25. The question: when a
layout is taught once on statement A, does a **different** statement B of the same issuer/layout import correctly on
first sight? The gate is at least 90% of second statements, with zero false accepts. This page is the measurement;
it does not change the engine.

## Verdict

| gate criterion | result | verdict |
|---|---|---|
| second statements import correctly after one teach, on the in-scope set | public spike set: **0 same-layout pairs**; synthetic pair set: **14 of 21 pairs = 66.7%** (8 of 12 distinct B statements), or **14 of 23 = 60.9%** (8 of 13) in the generator-profile view below | **NOT MET / NOT PROVABLE** |
| zero false accepts | **0** accepted statements carried a row that differed from the known rows | **PASS** on this corpus |
| same-statement pairs counted as proof | **0**; every pair below has two different statement ids and two different PDFs | **clean** |

The gate is not lowered and no pair is dropped: a pair whose A could not be taught is a failed pair, and it stays in
the denominator. The public half of the gate cannot be proven because there is no second statement to import; the
only importable same-layout pair set in the corpus is the synthetic one, and it misses the gate.

## Pair census

**Public spike set.** The 27 public files used by spikes 4 and 5 (11 importable statements and 16 expected-refuse
statements) are **27 distinct layouts under the engine's own fingerprint**. There are **0 same-layout pairs**, and
the 11 importable statements are 11 singletons. The four layouts that needed a template in spike 5 (the in-sample
templates) each appear exactly once. The public set therefore has no second statement to measure; this is the gap
the card names, not a result that teach-once passed. One public how-to insert on a vendor template was
considered and excluded: it shows several accounts and the engine refuses it as `multi_account` before any template
can be applied, so it is not an in-scope single-account second statement.

Spike 5's other synthetic files are singletons under the same fingerprint too: `bsb-001`, `bsb-003`, `bsb-005`
(the bsb set) and `C51` (resecta). A 52-file census — the 27 public files, those four, the vendor how-to insert and
the 20 agami files — gives 34 layout groups; only agami T1 and T2 have more than one statement (10 each), and those
are not importable (see below).

**Synthetic pair set.** The committed synthetic corpus has 20 statements, generated in the repository and pinned by
`sha256` in its manifest (the fixture directory's negative-control PDFs are not part of that manifest and are not
measured here). `templates.same_layout` groups them into 8 layouts: six with two or more statements and two
singletons. That gives 21 different-statement pairs, the only importable same-layout pairs available:

| layout | statements | pairs |
|---|---:|---:|
| AU | 4 | 6 |
| IN | 4 | 6 |
| SG | 3 (2 + a singleton) | 1 |
| US | 4 | 6 |
| BR | 2 | 1 |
| CL | 2 | 1 |
| CO | 1 | 0 |
| **total** | **20** | **21** |

Pairs are grouped by `templates.same_layout`, the exact predicate `templates.find` uses when it matches a saved
template against a new statement's masked fingerprint. Every row of the same-layout table below therefore has
`found: yes` by construction: the set can only contain a second statement whose fingerprint already matches A's. It
cannot see a same-issuer statement whose columns drifted. In this corpus that statement is `sg-02`: its generator
profile is SG like `sg-01` and `sg-03`, but its header roles are `date/desc/amount/balance` instead of
`date/desc/debit/credit/balance`, so it falls into its own fingerprint group. Both extra pairs were measured:

| extra pair (generator profile SG) | teach on A | found | B, template | B, cascade | B, first sight | false accept |
|---|---|---|---|---|---|---|
| sg-01 → sg-02 | accepted (columns only) | no | — | refused:does_not_reconcile (-) | refused:does_not_reconcile | no |
| sg-02 → sg-03 | refused:does_not_reconcile | no | — | — | correct | no |

The `(-)` is the runner's marker for a refused cascade: there is no parsed statement, so no path is reported. First
sight is always the generic path, with no template supplied.

By generator profile the set is 23 pairs: **14 import correctly after one teach = 60.9%**, and **8 of 13 distinct
second statements = 61.5%**. First sight without any teach is 5 of the 13 distinct second statements (38.5%). The
profile denominator is the more cautious one and it also misses the gate; the extra failures are counted, not
hidden. `sg-02` is also the SG singleton with the `DR`/`CR` marker gap below.

The spike-5 `agami` groups (ten statements each, two templates) are outside the committed corpus and not usable
here. Their shipped transaction data does not foot: for 19 of the 20 statements, opening plus the shipped rows
misses the shipped closing, and all 20 break their own running balance. The first statement of T1 is refused by
reconciliation (`does_not_reconcile`; the teacher gets the same code); T2 is refused earlier, at `no_header` (its
first pages, `00001`–`00003`, show no table header to either the generic parser or the teacher), so reconciliation
never runs on it. Neither group is an importable second statement. Owner statements were not read or used.

## Per-pair result

For every pair, the teacher sees only A. It reads A's header words and column extents from A's words, tries the
engine's template settings in a fixed simplest-first order, and accepts a setting only when **A itself reconciles**
(the product's rule). Nothing from B chooses the setting. B is then imported first-sight: `templates.find` must match
B's fingerprint to the template saved from A, and B is parsed with the saved config. The "cascade" column is the real
product path (generic parser first, then the saved template); the "template" column forces the saved config and shows
whether the taught mapping itself works.

| pair | layout | teach on A | found | B, template | B, cascade (used) | B, first sight | false accept |
|---|---|---|---|---|---|---|---|
| au-01 → au-02 | AU | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| au-01 → au-03 | AU | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| au-01 → au-04 | AU | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| au-02 → au-03 | AU | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| au-02 → au-04 | AU | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| au-03 → au-04 | AU | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| br-01 → br-02 | BR | refused:does_not_reconcile | no | — | — | refused:does_not_reconcile | no |
| cl-01 → cl-02 | CL | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| in-01 → in-02 | IN | accepted (row_start=date) | yes | correct | correct (template) | refused:does_not_reconcile | no |
| in-01 → in-03 | IN | accepted (row_start=date) | yes | correct | correct (template) | refused:does_not_reconcile | no |
| in-01 → in-04 | IN | accepted (row_start=date) | yes | correct | correct (template) | refused:does_not_reconcile | no |
| in-02 → in-03 | IN | accepted (row_start=date) | yes | correct | correct (template) | refused:does_not_reconcile | no |
| in-02 → in-04 | IN | accepted (row_start=date) | yes | correct | correct (template) | refused:does_not_reconcile | no |
| in-03 → in-04 | IN | accepted (row_start=date) | yes | correct | correct (template) | refused:does_not_reconcile | no |
| sg-01 → sg-03 | SG | accepted (columns only) | yes | correct | correct (generic) | correct | no |
| us-01 → us-02 | US | refused:unreadable_date | no | — | — | refused:unreadable_date | no |
| us-01 → us-03 | US | refused:unreadable_date | no | — | — | refused:unreadable_date | no |
| us-01 → us-04 | US | refused:unreadable_date | no | — | — | refused:unreadable_date | no |
| us-02 → us-03 | US | refused:unreadable_date | no | — | — | refused:unreadable_date | no |
| us-02 → us-04 | US | refused:unreadable_date | no | — | — | refused:unreadable_date | no |
| us-03 → us-04 | US | refused:unreadable_date | no | — | — | refused:unreadable_date | no |

`correct` means exact row count, ISO date and signed amount against the generator's own manifest. Refusals are the
engine's public reason codes; no statement text is printed or stored.

## Counts

| measure | value |
|---|---|
| statements / layouts / pairs | 20 / 8 / 21 |
| teach accepted / refused | 14 / 7 |
| accepted pairs whose saved template imports B correctly | 14 / 14 |
| pairs whose product cascade imports B correctly | 14 / 21 = 66.7% |
| distinct second statements (B) imported correctly after teach | 8 / 12 = 66.7% |
| generator-profile view: profiles / pairs / teach accepted / refused | 7 / 23 / 15 / 8 |
| generator-profile view: pairs imported correctly after one teach | 14 / 23 = 60.9% |
| generator-profile view: distinct second statements imported correctly after one teach | 8 / 13 = 61.5% |
| one teach per layout: second statements imported correctly | 8 / 12 = 66.7%; 8 / 8 among the four layouts where a teach was accepted |
| first sight **without** teach, statements | 9 / 20 = 45% |
| first sight **without** teach, distinct second statements | 5 / 12 = 41.7% |
| first sight **without** teach, pairs | 8 / 21 = 38.1% |
| false accepts | 0 |

The one-teach-per-layout view teaches on each layout's first statement and tests every other statement of that
layout. It is 8/8 on AU, IN, SG and CL; adding the two layouts where no teach was accepted (US and BR) makes it 8/12.
In the generator-profile view it is 8/13: the extra second statement is `sg-02`, which fails. Neither view is
reported as a gate pass.

## Why the seven same-layout pairs fail

- **US (6 pairs).** The statement prints its period day-first but each row month-first (for example a period written
  `01/03` to `31/03` around rows written `03/01`). Under one date order the period is impossible, under the other the
  rows fall outside it. The engine reports `date_outside_period` (surfaced as `unreadable_date`) and no template
  setting can override a printed period. This is a corpus/date-order conflict, not a column-mapping problem; the
  older spike parser used a per-country profile (US month-first) instead of cross-checking the printed period
  against the rows.
- **BR (1 pair).** The sign is a per-row `(+)`/`(−)` token in its own column. The template schema has no role for a
  sign-marker column, and `amount_sign` is one convention for the whole amount column, so the generic parser refuses
  (`ambiguous_sign` internally, surfaced as `does_not_reconcile`) and no teach setting can express the per-row
  marker. The SG singleton `sg-02` has the same `DR`/`CR` marker gap and no same-layout pair; its two
  generator-profile pairs are the extra rows above.

The IN layout is the case teach-once genuinely helps: the generic parser refuses all four statements
(`does_not_reconcile`), and a template taught with `row_start=date` imports all six pairs. The AU, SG and CL pairs
already import generically, and the product cascade keeps using the generic path there; their templates are correct
when forced, but they add no measured value on this corpus.

## Reproduce

```
env -u PYTHONPATH PYTHONPATH=$PWD/src .venv/bin/python tests/pdfteach_pairs.py
env -u PYTHONPATH PYTHONPATH=$PWD/src .venv/bin/python tests/pdfteach_pairs.py --group-by profile
```

It verifies every corpus `sha256`, groups with `templates.same_layout` (with `--group-by profile`, by the generator
profile instead, for the cautious view above), teaches on A only, saves into a temp SQLite database, and prints the
table. `--json PATH` writes the per-pair record; `--census DIR...` prints only layout group sizes for any directory
of PDFs (used for the 52-file census: 34 groups, 2 multi-statement). No network, no model call, no keys, no owner
statements.

Wall time on this host: **6.2–8.3 s** per run for either view (host load varies; one profile run under load took
34.7 s); the pairs and counts are identical across runs.

## Validation

`env -u PYTHONPATH PYTHONPATH=$PWD/src .venv/bin/python scripts/check.py fast --base 43de84c` on this branch
(tests and this page only; no `src/` change): **3926 tests, 19 failed, 11 skipped** (run twice, same result). All 19
failures are in `tests/test_cronpipe.py` and `tests/test_plugin_crons.py`; the same 19 node ids fail identically on
the clean base `43de84c` worktree, so they are the host's pre-existing Hermes/cron environment failures, not this
change. The prefix matters: the shared environment's editable install points at another worktree, and without
`PYTHONPATH=$PWD/src` the run loads the old 0.4.0 code. The two new teach-once tests pass, and the PDF import test
files pass (117/117). No browser test, gate or full-suite run was part of this card.

## Regression test

`tests/test_pdfimport_teach_once.py` builds two synthetic statements of one headerless layout with different periods,
openings, descriptions and amounts. It teaches on A (A reconciles), saves the template, proves B has no generic parse
(`no_header`), finds the template by B's fingerprint and imports B through the product cascade, asserting the exact
dates, signed amounts, opening and closing of B. A second test drives the product's `importer.commit(...,
teach=True)` path twice: a statement that does not reconcile is refused with `does_not_reconcile` and leaves no
template, transaction, account or import behind, while a control through the same path saves exactly one template
when the statement reconciles — so the zero counts are a result of the refusal, not of a teach path that never
saves. No skips, no weakened asserts.

## Assumptions

- "Imported correctly" is exact row count, ISO date and signed amount against the generator's manifest. Descriptions
  are not compared; the engine's own replay against B's printed balances is the import gate.
- "In-scope" is every statement in the committed corpus whose manifest expects an import (all 20). Excluding the
  layouts that turned out to be unteachable would be lowering the gate, so they stay in.
- Pairs are unordered, A is the lower id, and both statements are different PDFs. No statement is taught and tested
  on itself.
- The teacher is the runner's bounded search over the engine's template settings, using A's words only. It is not a
  human in the Teach screen and it does not use B's data. A production Teach screen may accept fewer settings than
  this search; that would make the counts worse, not better.
- Dates come from the statements as printed; no year was typed, no opening or closing was supplied by the user.

## Not proven / not run

- Not real bank statements: the synthetic corpus is generated in-repo. The public statements are all singleton
  layouts, so the public gate has no second statement at all.
- Not `check.py full`, the release gates, the browser tests or a paid model eval. `check.py fast` ran twice with the
  same result; its only failures are the pre-existing cron/environment tests above.
- Not a statement-text result: no merchant, person, account or statement line is quoted anywhere in this page or in
  the runner's output.
