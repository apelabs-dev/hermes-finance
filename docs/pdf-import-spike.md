# PDF import spike: measured gate report

## Spike 4: Docling + Haiku 5.5, balance-anchored

**HELD-OUT, anchored, `claude-haiku-5-5` on both arms: DA (Docling + Haiku, the owner's design)
reconciled 3 of 8; AH (text layer + Haiku, the control) reconciled 2 of 8. The gate is 7 of 8. Both
arms missed it, DA by 4 statements.** Neither arm returned a row the judge had not accepted:
`wrong_rows_returned` is 0 in every run. With the multi-account veto lifted, both arms reach 3 of 8
(`would_reconcile`), so the veto alone does not explain the gap.

The order was: TUNE (AH `a1`; DA `da1`, then `da2`), the design note, the freeze at commit
**`c570948`**, then HELD-OUT exactly once per arm at that sha. Both held-out result files record
`c570948`. A model-free `--rescore` of the saved held-out replies gives the same outcome on all 7 called
statements for both arms (s04 made no request, so it has no reply to re-score). No person opened,
read or printed the held-out files; the runner printed table lines only.

### HELD-OUT: arm DA (Docling 2.150.1 -> Haiku 5.5, `da2`)

| id | country | pages | sent | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list $ |
|---|---|---:|---|---:|---|---|---:|---:|---:|---:|
| s01 | AU | 1 | tables | 0 | refused:unanchored | false | 42.58 | 5725 | 1769 | 0.00161 |
| s02 | CA | 4 | tables | 45 | reconciled | true | 47.22 | 7448 | 5325 | 0.00373 |
| s03 | DE | 2 | tables | 12 | reconciled | true | 36.17 | 6224 | 6310 | 0.00398 |
| s04 | SG | 1 | — | 0 | refused:no_text_layer | — | 0.00 | — | — | — |
| s05 | US | 1 | tables | 0 | refused:no_balances | false | 13.83 | 5210 | 826 | 0.00103 |
| s06 | US | 1 | markdown | 0 | refused:unanchored | false | 9.39 | 6248 | 303 | 0.00098 |
| s07 | US | 2 | markdown | 0 | refused:multi_account | false | 15.54 | 6771 | 2065 | 0.00196 |
| s08 | CA | 1 | tables | 11 | reconciled | true | 25.71 | 5420 | 1438 | 0.00138 |

**3 of 8 reconciled (anchored).** would_reconcile true 3, false 4, — 1. Refused: `unanchored` 2, `no_text_layer` 1, `no_balances` 1, `multi_account` 1. Errored 0; wrong_rows_returned 0. Prompt `da2`, commit `c570948`.

### HELD-OUT: arm AH (text layer -> Haiku 5.5, `a1`)

| id | country | pages | sent | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list $ |
|---|---|---:|---|---:|---|---|---:|---:|---:|---:|
| s01 | AU | 1 | text | 0 | refused:unanchored | false | 10.03 | 5495 | 1930 | 0.00164 |
| s02 | CA | 4 | text | 45 | reconciled | true | 26.87 | 7194 | 7981 | 0.00501 |
| s03 | DE | 2 | text | 12 | reconciled | true | 11.41 | 13472 | 2509 | 0.00352 |
| s04 | SG | 1 | — | 0 | refused:no_text_layer | — | 0.00 | — | — | — |
| s05 | US | 1 | text | 0 | refused:multi_account | true | 8.47 | 6658 | 1364 | 0.00159 |
| s06 | US | 1 | text | 0 | refused:no_rows | — | 6.98 | 7268 | 751 | 0.00141 |
| s07 | US | 2 | text | 0 | refused:multi_account | false | 10.51 | 6782 | 1828 | 0.00185 |
| s08 | CA | 1 | text | 0 | refused:does_not_reconcile | false | 12.56 | 5659 | 2679 | 0.00205 |

**2 of 8 reconciled (anchored).** would_reconcile true 3, false 3, — 2. Refused: `unanchored` 1, `no_text_layer` 1, `multi_account` 2, `no_rows` 1, `does_not_reconcile` 1. Errored 0; wrong_rows_returned 0. Prompt `a1`, commit `c570948`.

### What the held-out refusals are

- **s04 (SG): `no_text_layer` on both arms, with no model call.** The selection check counted pdfplumber
  characters (more than 0), but the spike's own threshold is at least 10 non-blank characters. The file
  fell between the two. DA needs the text layer to anchor, so without one it refuses by design.
- **`unanchored`** means the reply printed an opening, closing or running balance that is not a money
  token in the PDF's text layer.
  - DA: s01, s06.
  - AH: s01.
  - On s06, DA sent Docling's markdown and got back figures the text layer does not print.
- **`multi_account`** means the judge's veto fired.
  - DA: s07.
  - AH: s05, s07.
  - On s05 AH would reconcile with the veto lifted. DA refused s05 as `no_balances` instead.
- **s08 (CA): DA reconciled where AH did not.** AH's rows did not add up to the printed balances
  (`does_not_reconcile`). DA's rows from Docling's table did.

### Cost, latency, memory

- **Per called statement, held-out.**
  - DA: 27.2 s wall, of which 12.3 s is the model and the rest is Docling (the first file includes
    model load, 42.6 s); $0.00209 list.
  - AH: 12.4 s wall (12.1 s model); $0.00244 list.
- **Totals, held-out.** DA $0.01466 and AH $0.01706 list, 7 calls each.
- **Docling peak RSS: 949.9 MB** (DA held-out process, which also loaded pdfplumber). AH's process peaked
  at 74.2 MB.
- **Whole card: 38 Haiku calls of the 70 budget, $0.0666 list.**
  - TUNE: AH 8, DA `da1` 8, DA `da2` 8.
  - HELD-OUT: AH 7, DA 7.
  - Every call is logged in `.dispatch/model-calls.md`, which is git-ignored.
- List price is what the CLI reports as `total_cost_usd`. A subscription is not this app's spend.

### Served-model proof

All 38 replies carry `modelUsage` keys of exactly `["claude-haiku-5-5"]`, saved with each request in
`.dispatch/replies/<arm>/<corpus>/<id>.json` (git-ignored, 0600) and logged in the call log's
`served_model` column. The runner exits 3 on any other served model, and none did. The pin is the
seam's own claude argv plus `--model claude-haiku-5-5`, set in `tests/pdfspike_run4.py` only.

### What this does NOT prove

- **It is not a measure of real user statements.** These are public sample and specimen PDFs. Most are
  credit-union "how to read your statement" pages, with annotation callouts and several accounts on one
  page. Real exports may be easier (one account, a clean table) or harder (long, multi-page).
- **8 files is a small sample.** The gap between 3 of 8 and 2 of 8 is one statement and is not a
  difference between the arms. The gap to 7 of 8 is the result.
- **The anchor proves a balance is printed somewhere in the text layer, not that it sits in the balance
  column** (spike 3's limit, unchanged).
- **Only one payload rule was tested**: tables-first, then markdown with picture text. Sending Docling's
  full markdown when it also found tables was not run (see s57 on TUNE).
- **One model was tested.** Nothing here says how Sonnet or Opus would score on the same files.
- **This is Docling 2.150.1 with OCR off only.** OCR on was not measured.

### Design note (written on TUNE, before the freeze)

- **Arms.** Both use `claude-haiku-5-5`, sent through the app's `hermes_finance.clibrain` seam
  (its own claude argv plus `--model claude-haiku-5-5`, set in `tests/pdfspike_run4.py` only; `src/`
  is unchanged), and both are anchored.
  - **DA**, the owner's design: Docling 2.150.1 (CPU, OCR off, table structure on, models from
    `$HF_DOCLING_ARTIFACTS`) -> Docling's tables as markdown, or its document markdown when it
    found no table -> one request, prompt `da2` -> spike 3's fixed JSON schema -> `require_printed_anchors`
    against the **pdfplumber text layer of the same PDF**, never against Docling's output -> the unchanged
    judge. When Docling yields nothing, the result is `refused:no_rows` with no model call and no
    text-layer fallback.
  - **AH**, the control: spike 3's arm A exactly (text layer -> `a1` -> anchor -> judge); only the model
    is pinned.
- **Served model.** Every reply's `modelUsage` keys are saved, and a reply served by anything other than
  `claude-haiku-5-5` stops the run. All TUNE replies were `claude-haiku-5-5`.
- **Prompt history (TUNE only).**
  - **`da1`**: spike 3's instructions, framing Docling's output as data, plus two schema-neutral rules:
    no opening/closing lines as rows, and copy figures, never compute them. Result: **1 of 8**, with 5
    `no_rows`. On 5 of 8 sample statements Docling sent 65 to 842 characters, against text layers of
    1,550 to 3,028. With OCR off, Docling's layout model labels the statement body as a *picture*, and
    the default `export_to_markdown()` drops the text-layer cells inside picture regions.
  - **`da2`**: the same prompt; the markdown export now uses `traverse_pictures=True`, so picture-region
    text is part of Docling's markdown. Docling's settings are unchanged. Result: **1 of 8**, with
    `would_reconcile` true on 2 and the refusals split across `multi_account` 4, `unanchored` 1 and
    `no_rows` 2. AH on the same 8: **1 of 8**, with `would_reconcile` true on 2.
- **Left as specified.** When Docling finds any table, DA sends only its tables. On s57 that is a single
  fee-summary table, so the transactions never reach the model (`no_rows`). The card's rule is
  "tables, or markdown when there are no tables", so the frozen runner keeps it. Sending the full
  markdown whenever tables exist was not tried.
- **What TUNE shows.** Most refusals on both arms are the judge's `multi_account` veto: these public
  samples are mostly credit-union statements that print a share and a checking stream on one page. The
  veto is the judge's and was not touched. `would_reconcile` lifts only that veto, as in spike 3.
- **Freeze.** `da2` / `a1` at the freeze commit below. HELD-OUT runs once per arm at that sha.

TUNE `da2` ran on the working tree that became the freeze commit, so its result file records the
parent `02d2290`. The freeze added only the `da2` export change, its test and this note.

### TUNE (in-sample): arm DA, `da2` (frozen)

| id | country | pages | sent | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list $ |
|---|---|---:|---|---:|---|---|---:|---:|---:|---:|
| s51 | US | 1 | tables | 0 | refused:multi_account | false | 38.94 | 6773 | 2194 | 0.00124 |
| s52 | DE | 1 | tables | 13 | reconciled | true | 17.41 | 5930 | 2155 | 0.00121 |
| s53 | CA | 1 | markdown | 0 | refused:multi_account | — | 8.01 | 5612 | 377 | 0.00089 |
| s54 | US | 1 | tables | 0 | refused:multi_account | false | 20.12 | 5835 | 1997 | 0.00113 |
| s55 | CA | 1 | markdown | 0 | refused:unanchored | false | 14.04 | 6470 | 1880 | 0.00181 |
| s56 | DE | 2 | markdown | 0 | refused:no_rows | — | 10.67 | 5234 | 315 | 0.00078 |
| s57 | US | 1 | tables | 0 | refused:no_rows | — | 9.32 | 5100 | 230 | 0.00024 |
| s58 | US | 2 | markdown | 0 | refused:multi_account | true | 14.75 | 6326 | 1830 | 0.00176 |

**1 of 8 reconciled (anchored).** would_reconcile true 2, false 3, — 3. Refused: `multi_account` 4, `unanchored` 1, `no_rows` 2. Errored 0; wrong_rows_returned 0. Prompt `da2`, commit `02d2290`.

### TUNE (in-sample): arm DA, `da1`

| id | country | pages | sent | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list $ |
|---|---|---:|---|---:|---|---|---:|---:|---:|---:|
| s51 | US | 1 | tables | 0 | refused:multi_account | false | 72.46 | 6774 | 3187 | 0.00252 |
| s52 | DE | 1 | tables | 13 | reconciled | true | 15.19 | 5930 | 2086 | 0.00180 |
| s53 | CA | 1 | markdown | 0 | refused:no_rows | — | 10.49 | 5005 | 245 | 0.00070 |
| s54 | US | 1 | tables | 0 | refused:multi_account | false | 19.87 | 5833 | 2001 | 0.00174 |
| s55 | CA | 1 | markdown | 0 | refused:no_rows | — | 6.96 | 5014 | 154 | 0.00066 |
| s56 | DE | 2 | markdown | 0 | refused:no_rows | — | 10.62 | 5066 | 114 | 0.00065 |
| s57 | US | 1 | tables | 0 | refused:no_rows | — | 10.76 | 5100 | 344 | 0.00077 |
| s58 | US | 2 | markdown | 0 | refused:no_rows | — | 10.07 | 5248 | 205 | 0.00073 |

**1 of 8 reconciled (anchored).** would_reconcile true 1, false 2, — 5. Refused: `multi_account` 2, `no_rows` 5. Errored 0; wrong_rows_returned 0. Prompt `da1`, commit `02d2290`.

### TUNE (in-sample): arm AH, `a1`

| id | country | pages | sent | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list $ |
|---|---|---:|---|---:|---|---|---:|---:|---:|---:|
| s51 | US | 1 | text | 0 | refused:multi_account | false | 9.19 | 6172 | 1623 | 0.00205 |
| s52 | DE | 1 | text | 13 | reconciled | true | 11.62 | 5646 | 1620 | 0.00152 |
| s53 | CA | 1 | text | 0 | refused:multi_account | — | 6.64 | 5556 | 563 | 0.00097 |
| s54 | US | 1 | text | 0 | refused:multi_account | false | 8.05 | 6267 | 1247 | 0.00145 |
| s55 | CA | 1 | text | 0 | refused:unanchored | false | 42.62 | 6470 | 10098 | 0.00592 |
| s56 | DE | 2 | text | 0 | refused:no_rows | — | 5.05 | 5091 | 337 | 0.00076 |
| s57 | US | 1 | text | 0 | refused:multi_account | false | 7.54 | 6200 | 1245 | 0.00144 |
| s58 | US | 2 | text | 0 | refused:multi_account | true | 13.40 | 6229 | 2687 | 0.00217 |

**1 of 8 reconciled (anchored).** would_reconcile true 2, false 4, — 2. Refused: `multi_account` 5, `unanchored` 1, `no_rows` 1. Errored 0; wrong_rows_returned 0. Prompt `a1`, commit `02d2290`.

### Corpus

- **Sources.** Public sample, specimen and how-to-read statement PDFs, picked by the written rule in
  `tests/fixtures/pdfspike/manifest-spike4.json` (E1-E7). Within each country, files are ordered by the
  sha256 of the URL, and the first eligible one fills HELD-OUT, then TUNE.
- **Amendment A1.** The base split gave only 6 HELD-OUT files, because the CH and CO candidates failed
  fetch or the text-layer check, and spike 3's TUNE PDFs are no longer on disk (320 PDFs scanned, none in
  `manifest-real.json`). A1 added a second ordered pass under the same rules plus E8 (deposit or
  transaction accounts only). Its rule was written before any A1 file was downloaded, and before any file
  was extracted.
- **No overlap with spikes 1-3.** No file's sha256 is in `manifest-real.json`. Nothing was pre-filtered
  on "has balances" or "looks easy".
- **HELD-OUT.** 8 files from 5 countries (AU, CA x2, DE, SG, US x3), never opened by a person. The runner
  prints table lines only.
- **TUNE.** 8 files (US x4, DE x2, CA x2), inspected.

**Privacy.** After the held-out runs the 8 held-out PDFs were deleted from `.dispatch/samples/raw4/`.
No PDF, URL or statement text is committed.

## Spike 3: Docling and the AI brain, judged by reconcile-or-refuse

**Held-out: arm A (AI brain) 4 of 8 statements reconciled (50%), measured without the balance
anchor; arm D (Docling) 0 of 8. The 80% target — 7 of 8 — was missed by both.** **Arm A held-out:
unanchored 4 of 8 vs anchored: unknown (replies not saved).** In the measured run, arm A's opening,
closing and running balances were the model's own figures and were never checked against the
statement text, so its reconcile checked the model against itself. The raw arm A replies from the
frozen `56486a9` run were not saved, so the held-out run cannot be re-scored with the anchor added
afterwards: the anchored held-out number is unknown — not 4 of 8, and not 0 of 8. Neither arm
returned a row the judge had not accepted: `wrong_rows_returned` is 0 for both arms on both sets.
The diagnostic below does not rescue the headline: **arm A reconciled 4 of 8 under the v1 rule
(unanchored), and 4 of 8 had rows that reconcile with the multi-account veto lifted**; **arm D was 0
of 8 under both readings**. The ship/cut recommendation rests on the v1 numbers, because the v1 rule
is what an importer would ship with; the relaxed count is a scope diagnostic, not a second
measurement.

The measurement order below is: arm D TUNE (in-sample, with the diagnostic; the diagnostic column
was filled by a model-free re-run, as the design note records) -> arm A TUNE pass 1 (`a1`,
in-sample) -> the design note -> freeze at commit **`56486a9`** -> HELD-OUT once per arm. Both
held-out runs record `56486a9` as their commit. TUNE is in-sample: the TUNE files were inspected
and tuned on. The held-out files were not opened, read or printed by a person, and each arm
measured them exactly once; the frozen runners printed only the tables below. Everything added
after that — the balance anchor, the arm D two-streams fix, saved replies and the offline re-score
— is code and documentation made at zero model cost; nothing was re-measured (see
[After the measurement](#after-the-measurement-the-balance-anchor-zero-model-calls)).

### The two arms

- **Arm D, Docling.** Docling 2.150.1, CPU only, OCR off, table structure on. Its table cells are
  mapped through the merged seam's own header, date and money conventions into `importfile.Row`,
  then the same judge runs. One converter per process, one statement at a time. Arm D tries every
  contiguous range of tables and keeps whichever one the judge accepts, so it uses the judge to
  pick a candidate as well as to check it.
- **Arm A, the AI brain.** pdfplumber's text layer -> one request through the app's tool-less
  `clibrain` seam to Claude Code -> one strict JSON object (fixed schema in code, never in the
  statement) -> the same judge. `PROMPT_VERSION = "a1"`; the prompt wording did not change in this
  spike. Statement text is framed as data, and statement content never chooses the schema.
- **Arm DA (Docling markdown -> AI brain) was dropped.** The two AI-fed arms would have needed 48
  Claude calls over the 24 statements, against the card's 40-call budget; it was not run.

### TUNE (in-sample, after tuning)

All 16 TUNE files were inspected and used while tuning. The arm D table is the frozen code with the
`would_reconcile` diagnostic; its outcomes are identical to the first arm D TUNE run (3 of 16,
374.2 s), and the diagnostic column was filled by a second, model-free pass over the same files
(368.1 s). The arm A table is pass 1, `a1`, as measured; it is not a second pass.

#### Arm D, TUNE

| id | country | pages | rows | outcome | would_reconcile | seconds |
|---|---|---:|---:|---|---|---:|
| c01 | US | 6 | 0 | refused:multi_account | false | 32.50 |
| c04 | US | 2 | 31 | reconciled | true | 13.30 |
| c05 | US | 1 | 21 | reconciled | true | 5.71 |
| c07 | DE | 39 | 0 | refused:multi_account | false | 84.37 |
| c09 | AU | 3 | 0 | refused:no_balances | false | 32.93 |
| c11 | SG | 4 | 0 | refused:no_balances | false | 16.38 |
| c14 | US | 28 | 0 | refused:does_not_reconcile | false | 136.70 |
| c29 | US | 5 | 0 | refused:no_rows | — | 7.13 |
| c31 | US | 1 | 0 | refused:no_rows | — | 0.62 |
| c32 | US | 1 | 0 | refused:no_rows | — | 0.87 |
| c33 | DE | 2 | 0 | refused:no_rows | — | 2.11 |
| c34 | DE | 2 | 0 | refused:does_not_reconcile | false | 4.42 |
| c35 | DE | 1 | 8 | reconciled | true | 3.84 |
| c36 | US | 3 | 0 | refused:no_rows | — | 17.74 |
| c38 | CA | 6 | 0 | refused:no_rows | — | 8.35 |
| c39 | US | 2 | 0 | refused:no_rows | — | 1.10 |

**Arm D TUNE: 3 of 16 reconciled (18.75%), in-sample; `wrong_rows_returned` 0; 368.1 s total
(23.0 s per statement, 0.6–136.7 s).** The two multi-account refusals (`c01`, `c07`) are the
structural kind spike 2 also refused. Their `would_reconcile: false` is not determined: it was
produced by the frozen code, in which "two disjoint table runs that each reconcile" came back as a
placeholder row with no balances, so on that path lifting the veto could only fail. Whether either
file's larger stream reconciles on its own is unknown.

#### Arm A, TUNE (pass 1, `a1`)

| id | country | pages | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list_usd |
|---|---|---:|---:|---|---|---:|---:|---:|---:|
| c01 | US | 6 | 0 | refused:multi_account | not captured | 46.85 | 9151* | 7428* | 0.1519344* |
| c04 | US | 2 | 31 | reconciled | true | 23.12 | 4940 | 3019 | 0.0629278 |
| c05 | US | 1 | 21 | reconciled | true | 14.86 | 4442 | 1635 | 0.0584 |
| c07 | DE | 39 | 0 | refused:multi_account | not captured | 27.68 | 65341* | 5201* | 0.118648* |
| c09 | AU | 3 | 62 | reconciled | true | 66.64 | 4725 | 8317 | 0.194304 |
| c11 | SG | 4 | 0 | refused:does_not_reconcile | not captured | 28.41 | 6215* | 4057* | 0.083935* |
| c14 | US | 28 | 0 | refused:multi_account | not captured | 65.68 | 28824* | 6237* | 0.1320568* |
| c29 | US | 5 | 0 | refused:multi_account | not captured | 119.34 | —* | —* | —* |
| c31 | US | 1 | 0 | refused:multi_account | not captured | 13.73 | 4919* | 1287* | 0.0282836* |
| c32 | US | 1 | 0 | refused:multi_account | not captured | 13.68 | 4883* | 1308* | 0.0286964* |
| c33 | DE | 2 | 0 | refused:multi_account | not captured | 7.51 | 4047* | 588* | 0.0141292* |
| c34 | DE | 2 | 12 | reconciled | true | 18.67 | 5520 | 2234 | 0.079004 |
| c35 | DE | 1 | 8 | reconciled | true | 10.01 | 3957 | 1109 | 0.044 |
| c36 | US | 3 | 0 | refused:multi_account | not captured | 62.44 | 8764* | 3986* | 0.083017* |
| c38 | CA | 6 | 0 | refused:multi_account | not captured | 12.98 | 7853* | 1093* | 0.0249904* |
| c39 | US | 2 | 0 | refused:no_rows | not captured | 6.97 | 14462* | 402* | 0.0124844* |

**Arm A TUNE: 5 of 16 reconciled (31.25%), in-sample, measured without the balance anchor;
`wrong_rows_returned` 0; 538.6 s total
(33.7 s per statement); 23,584 input and 16,314 output tokens and $0.4386 list price for the five
accepted statements.** The 11 refused statements carried no usage in the pass-1 records; one
token-backfill request per statement (same `a1` prompt, same outcome except `c29`, which hit the
old 300 s deadline on the backfill) produced 154,459 input and 31,587 output tokens and $0.6782
list price. All TUNE usage is therefore 178,043 input, 47,901 output, **$1.1168 list price**.
`*` rows use the backfill figures; their pass-1 outcome is unchanged. **`c29` has no token count:
neither request returned usage.** `would_reconcile` was not captured for the vetoed files in pass
1 — the rows were already gone — so the TUNE table shows their refusal without the diagnostic.

### Design note: the model's multi-account claim, and the counterfactual

Arm A's `multi_account` is the model's own claim, not a structural reading. On TUNE the model
claimed it for **9 of 16** files (`c01 c07 c14 c29 c31 c32 c33 c36 c38`); spike 2's structural
detector refused **3** (`c01 c07 c32`). The claim over-triggers, and held-out can only be measured
once, so the arm must not be judged on a prompt artifact that cannot be investigated afterwards.

The veto stays: spike 2 refused multi-account files too, the judge raises
`MultiAccountUnsupported`, and keeping it is what makes the arms comparable. Instead, the runner
records a counterfactual for every file at **zero model cost**: it runs the same `Candidate`
through the judge a second time with `multi_account=False` and records `would_reconcile` — `true`
or `false` when the candidate had rows, `null` when there was no candidate or no rows (a parse,
transport or engine refusal). A file the model vetoed can therefore appear as
`refused:multi_account` with `would_reconcile: true`: the arm read rows that reconcile, and the v1
scope rule — not the reconciler — refused it. (As measured, "reconcile" for arm A meant against the
model's own balances; since round 2 the counterfactual lifts only the veto, never the balance
anchor.)

Two runner-side changes came with it: `MODEL_DEADLINE_SECONDS` is 480 s (the token-backfill request
for the 5-page TUNE file `c29` hit the old 300 s deadline), and the counterfactual is recorded in
the JSON and the markdown table. `PROMPT_VERSION` stays `"a1"`: no prompt wording changed. Arm A
TUNE pass 1 predates the diagnostic, so its vetoed rows' counterfactual was not captured; the table
says so rather than guessing. The arm D TUNE table was re-run model-free to fill the column, and
its outcomes did not move.

### Freeze

The judge, both extractors, the runner and the contract tests were committed at **`56486a9`**
(`spike(pdf): frozen spike 3 runner with the multi-account counterfactual`, preceded by the judge
and extractor commits `03bd057` and `3b434a3`). Both held-out runs record `56486a9` as their
commit and UTC timestamps **2026-11-16T04:48:04+00:00** (arm D) and **2026-11-16T04:55:27+00:00**
(arm A). No `--force` exists in the protocol's command line; each result file is its own marker.

### HELD-OUT (one run per arm, not tuned)

Exactly one `--corpus heldout` run per arm, through the frozen runner, after sha256 verification
against `manifest-real.json`. No held-out statement text was read by a person or written out; the
runners printed only the tables.

#### Arm D, HELD-OUT

| id | country | pages | rows | outcome | would_reconcile | seconds |
|---|---|---:|---:|---|---|---:|
| c02 | US | 1 | 0 | refused:no_rows | — | 13.28 |
| c03 | US | 4 | 0 | refused:no_rows | — | 3.37 |
| c06 | CA | 1 | 0 | refused:does_not_reconcile | false | 0.98 |
| c08 | CH | 1 | 0 | refused:no_balances | false | 0.67 |
| c10 | AU | 3 | 0 | refused:no_rows | — | 4.65 |
| c12 | CO | 1 | 0 | refused:no_rows | — | 2.25 |
| c13 | US | 1 | 0 | refused:no_rows | — | 0.40 |
| c17 | DE | 2 | 0 | refused:no_balances | false | 2.14 |

**Arm D HELD-OUT: 0 of 8 reconciled (0%); `would_reconcile` true 0, false 3, null 5;
`wrong_rows_returned` 0; 27.8 s total (3.5 s per statement, 0.4–13.3 s).**

#### Arm A, HELD-OUT (measured without the balance anchor)

| id | country | pages | rows | outcome | would_reconcile | seconds | tokens_in | tokens_out | list_usd |
|---|---|---:|---:|---|---|---:|---:|---:|---:|
| c02 | US | 1 | 0 | refused:does_not_reconcile | false | 11.36 | 3992 | 993 | 0.04196 |
| c03 | US | 4 | 0 | refused:multi_account | false | 267.04 | 35975 | 27995 | 0.837864 |
| c06 | CA | 1 | 3 | reconciled | true | 6.40 | 3905 | 566 | 0.032724 |
| c08 | CH | 1 | 3 | reconciled | true | 8.50 | 4355 | 883 | 0.042664 |
| c10 | AU | 3 | 62 | reconciled | true | 75.45 | 4756 | 9362 | 0.215452 |
| c12 | CO | 1 | 0 | refused:does_not_reconcile | false | 16.24 | 4482 | 1941 | 0.06484 |
| c13 | US | 1 | 0 | refused:no_rows | — | 5.50 | 3816 | 219 | 0.025072 |
| c17 | DE | 2 | 16 | reconciled | true | 23.57 | 5473 | 2174 | 0.077428 |

**Arm A HELD-OUT: 4 of 8 reconciled (50%), measured without the balance anchor (anchored: unknown,
replies not saved); `would_reconcile` true 4, false 3, null 1;
`wrong_rows_returned` 0; 414.1 s total (51.8 s per statement, 5.5–267.0 s); 66,754 input and
44,133 output tokens, $1.338 list price (8,344 input and 5,517 output tokens, $0.1673 per
statement).**

**The diagnostic, per arm: arm A reconciled 4 of 8 under the v1 rule and 4 of 8 had rows that
reconcile when the multi-account veto is lifted (both unanchored); arm D reconciled 0 of 8 under
both readings.** The one held-out file the model vetoed (`c03`) would not have reconciled either,
so on this run the veto cost nothing; the 4-of-8 result is the extractor's, not the scope rule's.
The recommendation below rests on the v1 numbers.

### Refusals by reason, and wrong rows

| set | arm | multi_account | no_balances | does_not_reconcile | no_rows | errored |
|---|---|---:|---:|---:|---:|---:|
| TUNE | D | 2 | 2 | 2 | 7 | 0 |
| TUNE | A | 9 | 0 | 1 | 1 | 0 |
| HELD-OUT | D | 0 | 2 | 1 | 5 | 0 |
| HELD-OUT | A | 1 | 0 | 2 | 1 | 0 |

`wrong_rows_returned` is defined as a file with `rows > 0` whose outcome is not `reconciled`. It is
**0 for every arm and every set**, and it is **0 by construction**: both arms run the same judge
before returning, the judge returns only rows it has accepted, and a refusal records `rows = 0`.
The count therefore says "no extractor returned rows the judge rejected"; it does not say the rows
are economically right — see the limits below. For arm A as measured it says less still: the judge
compared the model's rows with the model's own balances, so a reply that dropped a row and
rewrote its closing to match would have counted as reconciled.

### After the measurement: the balance anchor (zero model calls)

Round 2 changed code and this document only. It made no model call, re-ran no held-out
measurement and read no held-out PDF (they are deleted). Measured numbers above are unchanged.

- **Arm A balances must be printed in the text.** Before the judge, arm A now refuses with
  `unanchored` unless the reply's opening, closing and every non-null running balance each match,
  to the cent and ignoring sign, a money token printed in the statement text that was sent
  (`printed_money_tokens` / `require_printed_anchors` in `tests/pdfspike_extract.py`, using the
  profile's existing amount parser). The anchor proves a figure is printed somewhere in the text,
  not that it is printed in the balance column: a statement whose text itself prints a fake figure
  (an injected line, say) passes the anchor, and only the judge's arithmetic is left. A number
  grouped with plain spaces, or printed with neither cents nor a currency sign, is not a money
  token, so it refuses rather than accepts. `unanchored` is an ordinary refusal reason in the
  runner's records, and its `would_reconcile` is `false`: lifting the multi-account veto never
  lifts the anchor. The judge (`tests/pdfspike_judge.py`) is unchanged.
- **Arm D's two streams.** Two disjoint table runs that each reconcile now return the larger one
  marked multi-account instead of a placeholder row, so `would_reconcile` can be `true` when a
  stream reconciles on its own.
- **Replies are kept for re-scoring.** For every arm A request it sends, the runner now writes the
  exact statement text and prompt sent and the raw reply to `.dispatch/replies/<corpus>/<id>.json`
  (git-ignored scratch, owner-only, never committed). `tests/pdfspike_run3.py --arm a --corpus
  <set> --rescore` re-judges those saved replies with the anchor, with zero model calls and no PDF
  read, into `spike3-a-<set>-rescore.json`. It refuses to run when no replies are saved, which is
  the case for every run so far, including the frozen held-out run.

### Install and runtime cost (arm D, Docling)

Measured on this Mac mini, CPU only, one statement at a time, no GPU and no remote worker:
disposable venv **1.2 GB**; model cache **506 MB**; first conversion **60 s** including the model
download; warm **~11 s for a 1-page statement**; peak RSS **~1.06 GB**. In the measured runs the
first file of each process also pays the model load (32.5 s for the 6-page `c01` on TUNE, 13.3 s
for the 1-page `c02` on held-out). The 16-file TUNE set took 368.1 s and the 8-file held-out set
27.8 s; the 28-page TUNE file alone took 136.7 s and the 39-page file 84.4 s. The venv and the
cache are disposable and are not part of the repo.

### The ceiling: what no extractor can fix

- **Multi-account statements are out of v1 scope.** Arm D's structural signal refused `c01` and
  `c07` on TUNE (2 of 16) and found none on held-out. Arm A's model claimed 9 on TUNE and 1 on
  held-out (`c03`); that is a claim, not a structural reading, and the TUNE over-trigger above is
  the evidence. No held-out file was structurally confirmed multi-account by arm D.
- **No printed balances cannot be checked, so they are always refused.** Arm D refused `c09` and
  `c11` on TUNE and `c08` and `c17` on held-out as `no_balances`. On held-out, arm A "reconciled"
  `c08` and `c17`, but arm A's balances are the model's own figures and were not checked against
  the text; spike 2 and arm D found no printed opening on `c17`. Whether those two statements carry
  the balances the judge needs is unproven either way.
- **Reachable denominator.** If the model's `c03` claim is right, 7 of the 8 held-out files are in
  v1 scope and the best possible score is **7 of 8 (87.5%)**, which would clear the 80% bar; if the
  claim is wrong, all 8 are reachable — less any file that prints no opening balance, which this
  run cannot settle for `c08` or `c17`. The measured arm A 4 of 8 is unanchored, so it is not a
  lower bound on an anchored score.

### Privacy (arm A)

Arm A sends the statement's text layer to a model provider (Claude Code, through the app's
tool-less CLI seam). Only the 24 public specimens were used; no real statement, no customer data
and no local file went to the provider. The prompt is a fixed extraction schema, and the statement
text is labelled as data. The CLI runs with no tools, no MCP servers, no session persistence and a
fresh empty working directory. The reply is parsed strictly and is never returned as rows unless
the judge accepts it. Since round 2 the runner also keeps each request's statement text and raw
reply locally in git-ignored, owner-only scratch (`.dispatch/replies/`) so they can be re-scored;
nothing there is committed.

### What spike 3 does NOT prove

- **Row-level correctness.** There is no hand-built ground truth for the rows. Reconcile is
  arithmetic: it proves the amounts add up to the printed balances, not that every row is the right
  transaction. TUNE `c05` (card statement, every sign reversed) reconciles and is counted; the
  standing caveat from spike 2 applies unchanged.
- **A general PDF importer.** 4 of 8 held-out statements reconciled with the best arm (50%,
  measured without the balance anchor), short of the 80% target. TUNE is in-sample (5 of 16 for
  arm A, also unanchored; 3 of 16 for arm D).
- **That arm A's balances are the statement's.** As measured, arm A's opening, closing and running
  balances came from the model and were not checked against the text. The anchored held-out number
  is unknown, because the replies were not saved; the anchor itself is proven only on synthetic
  replies in the contract tests.
- **That the model's `multi_account` claim is ground truth.** It over-triggered 9-to-3 on TUNE.
  The counterfactual is a scope diagnostic, not a corrected claim.
- **Docling at scale, or on another machine.** One version (2.150.1), CPU only, OCR off, one
  converter per process, one run per set on this Mac mini. The 39-page TUNE file took 84 s and the
  28-page file 137 s; the install is 1.2 GB plus a 506 MB model cache.
- **One model, one prompt, one run.** One Claude Code model behind a subscription, one prompt
  version (`a1`), one held-out run per arm. No error bars, no second sample.
- **That the arms are equal.** Both feed the same judge, but arm D is local and deterministic while
  arm A is a sampled model whose output varies between calls.

### Recommendation for 0.4.0

**Do not ship a general PDF importer on this evidence; ship narrow on arm A, or cut — the owner
decides.** The AI brain reconciled 4 of 8 held-out statements (50%, measured without the balance
anchor; anchored: unknown) and 5 of 16 in-sample; Docling reconciled 0 of 8 and 3 of 16. Neither
meets the 80% target for a general importer.

If a PDF path ships in 0.4.0, it should be **arm A, narrow and opt-in**: text-layer statements of
1–3 pages, one account, with a printed opening and closing balance, behind the same
reconcile-or-refuse judge and with the CSV/OFX fallback always visible. **A precondition of any
narrow arm A ship is that the balances are anchored to printed money tokens in the statement text**
(the round 2 check); without it the judge checks the model against itself. The four held-out files
arm A reconciled are in that shape by page count (`c06`, `c08` and `c17` are 1–2 pages; `c10` is 3
pages), but they were accepted without the anchor, and `c17` may print no opening balance at all.
A narrow-ship decision should wait for an anchored held-out number on fresh statements, or be made
knowing that none exists. A narrow path costs one model call per statement and a privacy notice;
it does not need Docling's 1.2 GB install.

If the owner wants the 80% general bar before any PDF path ships, **cut PDF import from 0.4.0** and
keep CSV/OFX. Arm D is not a candidate: 0 of 8 held-out, 3 of 16 TUNE, and its misses (`no_rows` on
5 of 8 held-out) are extraction failures, not scope refusals.

## Spike 2: reconcile-or-refuse and held-out real layouts

**Clean held-out: 0 of 6 files reconciled (0%), over 5 countries. The 80% target was missed.** The
set is below the card's minimum of 8 because `c02` and `c12` were in spike 1's local set (spike 1's
`local-08` and `local-07`), where they were inspected and tuned on, so they are not held out; `c10`
is a variant of TUNE `c09` from the same third-party project. Out of the full 8-file set the
recorded result is 0 of 8.

**The hard bar was met arithmetically: 0 files returned rows that did not reconcile, in either real
set or in any synthetic fixture.** Arithmetic is not economic correctness: `c05` is the
counterexample (its signs are reversed under the bank-account convention). The frozen parser was
measured once, on 2026-11-16 at 00:45:28 UTC; the six clean held-out files have not been opened or
re-measured since.

The order of the work below is the order it happened: split the corpus, tune on TUNE (in-sample),
freeze the parser, then run HELD-OUT exactly once. The TUNE number is in-sample after tuning; the
held-out number is the only out-of-sample result in this document.

### The two jobs

1. **Reconcile-or-refuse.** `rows()` now checks its own output against the statement before it
   returns, and raises instead of returning rows it cannot check. Two new stable codes:
   * `does_not_reconcile` (`ReconcileMismatch`) — rows were produced but do not reconcile.
   * `no_balances` (`NoBalances`) — there is no printed opening balance to anchor the first row, or
     neither a printed opening/closing pair nor a usable running balance, so there is no oracle.
   The check is `|opening - sum(amounts) - closing| <= 0.01` (app sign: money out > 0), plus the
   per-row check `balance[i-1] - amount[i] == balance[i]` when a running balance is printed on
   every or nearly every row. The existing sign-flip fallback is kept, but a flipped reading is
   accepted only when it satisfies every available check. `NoRows` still covers a PDF with no
   recognisable table or no transaction rows.
2. **Widen against TUNE, then measure HELD-OUT once.** The parser learned two-row headers, a
   description column inferred from geometry when its label is missing, value-driven money-column
   assignment, yearless dates inferred from the printed statement period, and total-only checking
   for statements without a running balance. Six new synthetic fixtures pin those structures, and
   two more pin the regressions below.

### Regression that forced the first job

Three committed fixtures carry this, and all were re-run against the unmodified spike-1 seam
(`c2725ef:tests/pdfspike_layout.py`, the same code as the reviewer's scratch copy) and against the
frozen one:

- **`x-off-cent` is the silent-wrong-rows case.** Spike 1 returned **6 rows with a 2-cent
  discrepancy against the statement's own opening and closing balance, and raised no exception**;
  the frozen seam raises `ReconcileMismatch` (`does_not_reconcile`) with the CSV/OFX message.
  `test_closing_balance_mismatch_beyond_the_tolerance_is_refused` pins it; a one-cent rounding
  difference stays inside the stated `0.01` tolerance by design.
- **`x-misaligned` is the same layout family in landscape**: 14 transaction rows, header labels
  drawn at the left edge of each column while the values under them are right-aligned, plus a
  debit/credit column pair. Spike 1 **refused it outright** (`no_rows`): its header-centre scoring
  could not match those cells, so it never got as far as returning rows, which means it is
  **not** the reviewer's case. The frozen seam reads it as 14 rows that reconcile to the printed
  opening and closing exactly (`delta = 0`), so the geometry defect is fixed, not merely guarded.
- **`x-reviewer-left` reproduces the reviewer's case**: A4 portrait, AU, 14 rows, `Date /
  Description / Debit / Credit / Balance`, opening and closing printed above the table, header
  labels at each column's left edge and values right-aligned. On the spike-1 seam it returned
  **8 of 14 rows with 0 of 14 amounts right and no exception** — the reviewer's reported shape. The
  pre-rework HEAD (`33ba603`) and the head seam both read it as **14 rows that reconcile exactly**
  (`delta = 0`). `test_reviewer_portrait_layout_reconciles_or_refuses` pins the row count and every
  amount, or an acceptable `does_not_reconcile`.

On the committed corpus the spike-1 seam parsed all 20 synthetic statements exactly, refused five
of the six layouts committed at that time outright, and its committed silent-wrong-rows results are
`x-off-cent` (2 cents) and `x-reviewer-left` (8 rows, every amount wrong). The committed
`test_misaligned_header_reconciles_or_refuses` therefore fails on the old seam with `NoRows` rather
than with a row mismatch, and passes on the new one by reconciling.

### Corpus and split

24 public text-layer statement PDFs from 7 countries (US, CA, DE, CH, AU, SG, CO), ids `c01`..`c39`
(not contiguous). Twelve of the 24 files came from outside the card's R2 source list, because that
list could not reach 20 without the dropped converter-site samples. The split was assigned by hand
with the research table's per-file descriptions (pages, type, PII status) in view, to maximise
country spread in HELD-OUT, and written and committed **before any file was opened**: v1 in
`9afde05`, then revised once in `c2725ef` when eight samples were dropped for reading like real
customers' statements and eight replacements were added **to TUNE only**; the held-out ids did not
change. TUNE is 16 files, HELD-OUT is 8. `manifest-real.json` carries only ids, set, sha256 and
byte counts; the PDFs themselves are never committed.

TUNE provenance is mixed: bank-published specimens and how-to-read guides, plainly-fictional demo
statements, other projects' synthetic fixtures, a government form exhibit, pedagogical examples and
an academic coursework reproduction. The held-out set is the same mix, with two exceptions: `c02`
and `c12` had been inspected in spike 1 and are therefore not held out, and `c10` is a variant of
TUNE `c09` from the same third-party project.

### TUNE (in-sample, after tuning)

All 16 TUNE files were inspected and used for tuning. Re-run at the frozen commit:

| id | country | pages | rows | outcome |
|---|---|---:|---:|---|
| c01 | US | 6 | 0 | refused:multi_account |
| c04 | US | 2 | 31 | reconciled |
| c05 | US | 1 | 21 | reconciled |
| c07 | DE | 39 | 0 | refused:multi_account |
| c09 | AU | 3 | 0 | refused:does_not_reconcile |
| c11 | SG | 4 | 0 | refused:no_balances |
| c14 | US | 28 | 0 | refused:no_rows |
| c29 | US | 5 | 0 | refused:no_rows |
| c31 | US | 1 | 0 | refused:no_rows |
| c32 | US | 1 | 0 | refused:multi_account |
| c33 | DE | 2 | 0 | refused:no_rows |
| c34 | DE | 2 | 0 | refused:no_rows |
| c35 | DE | 1 | 8 | reconciled |
| c36 | US | 3 | 0 | refused:no_rows |
| c38 | CA | 6 | 0 | refused:no_rows |
| c39 | US | 2 | 0 | refused:no_rows |

**TUNE: 3 of 16 reconciled (18.75%), of which `c05` has every sign reversed (card statement), so 2
of 16 are usable; 13 refused.** This is in-sample: the synonyms, geometry and header handling were
chosen after looking at these files.

`c05` is a card-style statement: its balance rises with spending, so every sign is reversed under
the bank-account convention. It reconciles arithmetically and is counted in the 3 of 16, but it is
not economically usable. **The "0 wrong-rows" count is arithmetic only.**

`c38` is the case that shows why the total-only oracle is weak. Before the description sanity guard
it returned two rows that netted to zero and therefore "reconciled" against a statement whose
opening and closing balances were equal. The rows were page furniture, not transactions, so a row
whose description is implausibly long or wordy is now refused as `no_rows` rather than returned.

### Freeze

The parser and corpus were frozen at commit **`7e52d4f`**. The only later commit before measurement,
**`3bd7afa`**, changes the runner's mapping from the `heldout` mode to the manifest's `held` set; it
does not touch `pdfspike_layout.py` or any fixture. The held-out runner records HEAD `3bd7afa` and
UTC timestamp **2026-11-16T00:45:28+00:00**.

### HELD-OUT (one run, not tuned)

Exactly one `--corpus heldout` run was made, through the frozen runner, after sha256 verification
against `manifest-real.json`. No `--force` was used; the marker `.dispatch/heldout-run.json` now
exists.

| id | country | pages | rows | outcome |
|---|---|---:|---:|---|
| c02 | US | 1 | 0 | refused:no_rows |
| c03 | US | 4 | 0 | refused:no_rows |
| c06 | CA | 1 | 0 | refused:does_not_reconcile |
| c08 | CH | 1 | 0 | refused:no_rows |
| c10 | AU | 3 | 0 | refused:does_not_reconcile |
| c12 | CO | 1 | 0 | refused:no_rows |
| c13 | US | 1 | 0 | refused:no_rows |
| c17 | DE | 2 | 0 | refused:no_balances |

**HELD-OUT: 0 of 8 reconciled (0%).** `c02` and `c12` were opened in spike 1 and are not held out,
so the clean held-out set is `c03 c06 c08 c10 c13 c17` → **0 of 6 over 5 countries**; `c10` is a
variant of TUNE `c09`. The bar was 80% (7 of 8 to clear 6.4/8); it was not met.
**Wrong-rows-returned: 0.** A wrong-rows result is a file where `rows > 0` and the returned rows do
not reconcile; the runner computes that count from the seam's printed opening/closing and the
committed expectations, and the seam itself now raises `ReconcileMismatch` before returning such
rows. The same count is 0 across the 20 synthetic gate files and the eight extra fixtures.

### Changes after the measurement

The seam changed after the frozen run, in commit **`3cc7dc6`**:

- **No printed opening balance, no rows.** The seam used to derive a missing opening balance from
  the first row's own amount and the first printed running balance. That let transaction 1 certify
  itself: a wrong first amount produced a matching derived opening and was returned with no error.
  The derivation is gone. With no printed opening balance the seam now raises `NoBalances`
  (`no_balances`) instead of returning rows. The closing derivation from the last printed running
  balance stays, because with a printed opening every row is checked transitively against the
  printed balances. Fixture `x-no-opening` pins the new refusal.
- **The same commit keeps the synthetic gate at 20/20.** The BR, CL and CO profiles now also
  recognise the English `Opening Balance` / `Closing Balance` labels that the committed synthetic
  statements print; three gate files relied on the deleted derivation for their opening and would
  otherwise have refused. No TUNE file uses those profiles, and the only BR/CL/CO file in the
  held-out set is `c12` (CO), which refused `no_rows` and is not held out, so this fallback cannot
  change a clean held-out result.

Deleting the derivation can only add refusals, and the label fallback only restores anchors for
the three synthetic gate files above. The recorded held-out result is therefore unchanged: it was
**not** re-measured, and exactly one held-out run exists (`.dispatch/heldout-run.json`). TUNE was
**not** re-measured either, because the samples were deleted; the TUNE table above remains the
frozen-commit result.

### What a refused user would see

These are the literal messages. Every one ends with an action.

| code | message |
|---|---|
| `does_not_reconcile` | This statement's transactions don't add up to the balance it prints, so the import can't trust what it read. Nothing was imported. Download the transactions as CSV or OFX instead. |
| `no_balances` | This statement doesn't print an opening balance, so the import can't check that the first transaction was read correctly. Nothing was imported. Download the transactions as CSV or OFX instead. |
| `no_rows` (no table) | This PDF has text but no transaction table in it (no date, description and amount columns were found). Download the transactions as CSV or OFX instead. |
| `no_rows` (no rows) | This PDF has text but no transaction rows in it. Download the transactions as CSV or OFX instead. |
| `no_rows` (unclean table) | This PDF's text doesn't form a clean transaction table, so the import can't read it reliably. Download the transactions as CSV or OFX instead. |
| `multi_account` | This PDF holds more than one account or balance stream. This import supports one account per file; download each account separately. |
| `password_required_or_wrong` | This PDF is password-protected, and the password is missing or wrong. Enter the password your bank uses, or download the transactions as CSV or OFX instead. |
| `no_text_layer` | This PDF has no text layer: it looks scanned, export CSV/OFX instead. The import reads text, not pictures, so a scan needs OCR first. |

`x-no-opening` pins the wording above: it prints a running balance on every row but no opening
balance. A statement that does print an opening balance but has neither a closing balance nor a
usable running balance still gets the older wording: "This statement doesn't print an opening and
closing balance or a running balance, so the import can't check that it read the transactions
correctly. Nothing was imported. Download the transactions as CSV or OFX instead."

### Held-out discipline

None of the six clean held-out files was opened, read, printed, screenshotted, or passed through
`extract_text()` by a human or a model in this worktree. Their metadata (`held-meta.json`) was read
only for id, country, profile, page count and provenance. The only operation on the PDFs was the
frozen runner's `layout.parse()` on the bytes after the sha256 check, which prints only id, country,
pages, rows, reconciled, outcome and seconds. The clean held-out ids are `c03 c06 c08 c10 c13 c17`.
`c02` and `c12` had been opened in spike 1 (`local-08` and `local-07`) and are not held out; `c10`
is a variant of TUNE `c09`. The six clean files remain held out.

### Provenance and honesty

- 24 files, 7 countries; 12 of the 24 came from outside the card's R2 source list, because that list
  could not reach 20 without the dropped converter-site samples. 8 samples were dropped because they
  read as real customers' statements and replaced only in TUNE. The split was assigned by hand with
  the research table's per-file descriptions (pages, type, PII status) in view, to maximise country
  spread in HELD-OUT, and committed before any file was opened (`9afde05` v1, then `c2725ef` final).
- The TUNE numbers are in-sample after tuning on those same files. The held-out numbers are not
  in-sample and are the only unbiased measurement here.
- The held-out result (0 of 6 clean, 0 of 8 in the set) is worse than TUNE (3 of 16, 2 of 16
  usable). That gap is the honest measure of how much the tuning generalised: not enough. Most
  held-out and TUNE files are guides, forms, investment reports, multi-account packs, or statements
  whose continuation pages do not repeat a header; the parser now refuses them cleanly instead of
  guessing.

### What spike 2 does NOT prove

- **Real-world usability.** 0 of 6 clean held-out files parsed (0 of 8 in the set). The contract is
  that the seam refuses when it cannot anchor the first row against a printed opening balance and
  check the rest against the statement's own balances; that contract held, but a refusal rate of
  100% is not a working importer.
- **That reconcile-or-refuse is enough.** A total-only statement can still pass with a subset of
  rows whose amounts happen to net to the printed change. The per-row running-balance check is the
  one that catches a mis-assigned column; when no running balance is printed, the total pair is the
  only oracle and it is weaker. `c38` above is a concrete example, caught by a description guard
  rather than by the balance check.
- **Account-type semantics.** The parser does not know whether a statement is a deposit account or
  a credit card. A card-style balance (rising with spending) can reconcile arithmetically under the
  bank-account sign check while the economic signs are reversed; TUNE `c05` is such a case. The gate
  is arithmetic reconciliation, not a guarantee that every sign matches the app's account
  convention.
- **Coverage of the real layouts that remain.** Multi-account packs, investment reports, forms,
  guides and continuation pages without a repeated header were refused, not parsed. The parser
  still expects a date column, some description text, and an amount family in a header block.
- **No OCR, no field-mapping UI, one account per file**, as in spike 1.

The sections below are spike 1's baseline report, kept unchanged.

## Verdict

- **Synthetic gate: PASS — 20/20.** Bar was 16/20; 7 country labels against 6 required.
- **Clean out-of-sample: 0 of 8.** Eight public statement samples, parsed by the parser exactly as built
  against the synthetic corpus, produced nothing: 0 of 8 parsed into rows and 0 of 8 matched a header
  under their country profile.
- **Second pass, after tuning on those same files: 2 of 8 parse, 1 reconciles.** Six generic label
  synonyms and a rewritten header-cell score were added after inspecting those eight files, so the
  recorded result is *in-sample* for the added labels. It is reported, but it is not an out-of-sample
  number.
- **What the 20 are:** 8 distinct header signatures over 5 column families, from one generator: one
  page size (A4 landscape), one font family (Helvetica, regular and bold), and in every file each
  header label is drawn with the same alignment as the values beneath it. US and CO share the `signed`
  geometry and AU and SG share `split`, so the corpus does not satisfy the brief's "no two countries
  sharing a layout" constraint even though it clears the card's numeric bar.

## What was measured and why

The question for this spike was narrow: **does `pdfplumber` parse real-world-shaped, text-layer bank
statements well enough to justify building an importer?** The deliverable is a measurement, not an
importer. `tests/pdfspike_layout.py` exposes the seam the production card would lift:

```python
rows(pdf_bytes: bytes, password: str | None = None, *, profile: str) -> list[Row]
```

It returns `hermes_finance.importfile.Row` objects: ISO date, whitespace-collapsed description, amount in
the app's sign (money out positive, rounded to cents), `account=None`, `fitid=None`, `line` = transaction
index, `trntype=None`. `pdfplumber` is imported lazily inside the call, so the repository's own venv skips
the test module instead of failing at collection. A missing engine raises `PdfEngineMissing`; encrypted,
scanned, multi-account and no-row files raise their own categories (see the negative controls below).

The parser never reads `manifest.json`. It reads the PDF's text layer and its own printed opening and
closing balances. Country profiles (`AU IN SG US BR CL CO`) carry **conventions only**: date order and
formats, decimal and thousands separators, header label variants, sign style, opening/closing/summary
words. No profile carries a row count, an amount, a bank name, a page count or anything else
file-specific, and every file of a country shares one profile.

`pdfspike_layout.py` reuses `importfile._parse_amount`, `_parse_date` and `_cents`. The extensions live
inside that module, not in `importfile.py`, which is untouched: the per-country order of date formats
(which is what resolves an ambiguous MDY/DMY date), header-cell scoring and column assignment,
stacked-row assembly, CR/DR and `(+)`/`(-)` mapping, and the internal printed-balance reconciliation.

## Environment and runtime

| | |
|---|---|
| pdfplumber | 0.11.10 (the version installed and exercised) |
| Python | 3.12.13 |
| OS | macOS 27.0.1 arm64 |
| pdfminer.six | 20260107 |
| fpdf2 (corpus generator) | 2.9.0 |
| Synthetic corpus runtime | **0.314 s total**, 0.009–0.050 s per statement |
| Run date | 2026-11-15 |

The disposable venv lives at `.dispatch/venv`; `pdfplumber` and `fpdf2` are not repository dependencies.
The runner reads the pdfplumber version from the module and writes it to `results.json`.

## Corpus and gate

20 committed text-layer PDFs across 7 countries, generated by `tests/pdfspike_corpus.py` and pinned by
`sha256` in `tests/fixtures/pdfspike/manifest.json`; the runner refuses to measure a PDF whose committed
bytes do not match. Each statement prints an opening balance, a closing balance and a per-row running
balance. A file passes only when **both** the row count is exact **and** the parsed amounts reconcile that
statement's own printed opening to closing.

| id | country | layout features | expected | found | delta | verdict |
|---|---|---|---:|---:|---:|---|
| au-01 | AU | multi-line description, separate debit/credit columns, preamble noise above the table, opening-balance row inside the table, decimal dot + comma thousands | 14 | 14 | +0.00 | pass |
| au-02 | AU | multi-line description, separate debit/credit columns, preamble noise above the table, opening-balance row inside the table, decimal dot + comma thousands | 18 | 18 | +0.00 | pass |
| au-03 | AU | separate debit/credit columns, preamble noise above the table, repeated page header, opening-balance row inside the table, page break in the middle of the table, decimal dot + comma thousands | 34 | 34 | +0.00 | pass |
| au-04 | AU | separate debit/credit columns, preamble noise above the table, opening-balance row inside the table, ambiguous MDY/DMY date, decimal dot + comma thousands | 12 | 12 | +0.00 | pass |
| in-01 | IN | cells stacked one-per-line, separate debit/credit columns, preamble noise above the table, repeated page header, Indian lakh grouping, decimal dot + comma thousands | 12 | 12 | +0.00 | pass |
| in-02 | IN | cells stacked one-per-line, separate debit/credit columns, preamble noise above the table, repeated page header, Indian lakh grouping, decimal dot + comma thousands, non-amount parenthesised document number | 16 | 16 | +0.00 | pass |
| in-03 | IN | cells stacked one-per-line, separate debit/credit columns, preamble noise above the table, repeated page header, page break in the middle of the table, Indian lakh grouping, decimal dot + comma thousands | 20 | 20 | +0.00 | pass |
| in-04 | IN | multi-line description, cells stacked one-per-line, separate debit/credit columns, preamble noise above the table, repeated page header, Indian lakh grouping, decimal dot + comma thousands | 10 | 10 | +0.00 | pass |
| sg-01 | SG | CR/DR suffix, separate debit/credit columns, preamble noise above the table, summary/total lines with an explicit sign, decimal dot + comma thousands | 12 | 12 | +0.00 | pass |
| sg-02 | SG | DR/CR marker column, preamble noise above the table, decimal dot + comma thousands | 14 | 14 | +0.00 | pass |
| sg-03 | SG | CR/DR suffix, separate debit/credit columns, preamble noise above the table, repeated page header, page break in the middle of the table, decimal dot + comma thousands | 26 | 26 | +0.00 | pass |
| us-01 | US | trailing minus, preamble noise above the table, decimal dot + comma thousands | 14 | 14 | +0.00 | pass |
| us-02 | US | parentheses negative, preamble noise above the table, decimal dot + comma thousands | 14 | 14 | +0.00 | pass |
| us-03 | US | multi-line description, trailing minus, parentheses negative, preamble noise above the table, decimal dot + comma thousands | 18 | 18 | +0.00 | pass |
| us-04 | US | preamble noise above the table, summary/total lines with an explicit sign, ambiguous MDY/DMY date, decimal dot + comma thousands | 16 | 16 | +0.00 | pass |
| br-01 | BR | (+)/(-) token, preamble noise above the table, decimal comma + dot thousands | 12 | 12 | +0.00 | pass |
| br-02 | BR | (+)/(-) token, preamble noise above the table, opening-balance row inside the table, decimal comma + dot thousands | 16 | 16 | +0.00 | pass |
| cl-01 | CL | separate debit/credit columns, preamble noise above the table, decimal comma + dot thousands | 12 | 12 | +0.00 | pass |
| cl-02 | CL | separate debit/credit columns, preamble noise above the table, opening-balance row inside the table, decimal comma + dot thousands | 16 | 16 | +0.00 | pass |
| co-01 | CO | preamble noise above the table, decimal dot + comma thousands | 14 | 14 | +0.00 | pass |

**GATE: PASS  20/20** (16/20 required; 7 countries, 6 required).

The 20 statements are 8 header signatures over 5 column families (separate debit/credit columns — the
CL files use this family at narrower spacing; single signed amount; sign token; `DR/CR` marker column;
stacked one-cell-per-line). They were produced by one `_Doc` generator class with one page size
(A4 landscape), one font family (Helvetica), and header labels drawn with the same alignment as the
values under them.

The manifest's `required_features` list has 18 entries and the generator fails if any is missing. The
committed corpus covers all 18: multi-line description; cells stacked one-per-line; CR/DR suffix; DR/CR
marker column; separate debit/credit columns; `(+)`/`(-)` token; trailing minus; parentheses negative;
preamble noise; repeated page header; opening-balance row inside the table; summary/total lines with an
explicit sign; page break in the middle of the table; Indian lakh grouping; decimal comma + dot
thousands; decimal dot + comma thousands; ambiguous MDY/DMY date; and a non-amount parenthesised document
number.

### Reconciliation sign

The brief writes the check as `opening + sum(amounts) == closing` with amounts in the app's sign (money
out positive). Those two statements are not algebraically compatible: with money out positive, the
balance falls when the sum rises, so the correct check is `opening - sum(amounts) == closing`. The seam
returns the app's sign (a withdrawal is positive, a deposit negative), so this report and the runner use
`opening - sum(amounts) == closing`, tolerance 0.01. `results.json` records the app-sign check, the
equivalent money-in-positive form, and the tolerance, so the intent is explicit rather than silently
reinterpreted. This is the one deviation from the brief's literal formula.

## Negative controls

Committed, not part of the 20-file denominator, and exercised by the runner and the pytest module.

| control | observed |
|---|---|
| `locked.pdf`, no password | `password_required_or_wrong` |
| `locked.pdf`, wrong password | `password_required_or_wrong` |
| `locked.pdf`, correct password | `rows:4` |
| `scan.pdf` (no text layer) | `no_text_layer`; the message says "looks scanned, export CSV/OFX instead" |
| `multi_account.pdf` (two balance streams) | `multi_account` |
| `empty.pdf` (text, no transactions) | `no_rows` |

The locked PDF is AES-128 encrypted with a user password that lives only in `tests/pdfspike_corpus.py`
and the test module, never in a filename, the manifest or a log. "Missing" and "wrong" are one
user-facing category by design.

## Local out-of-sample

Eight public text-layer samples from the vetted list in the research note — six from a public converter
site, one bank-published sample and one university coursework PDF — were fetched into the git-excluded
local corpus directory, parsed with the country profile nearest to each sample's source, and then
deleted. They were never committed and never entered the corpus. `results.json` holds only ids,
countries, page counts, row counts, booleans and seconds.

1. **First pass, parser as built:** 0 of 8 produced rows, and 0 of 8 matched a header under their
   country profile.
2. **Tuning:** those eight files' header labels were inspected, six generic label synonyms were added,
   and the header-cell score was rewritten (run-history row 6).
3. **Second pass:** the recorded result.

| | first pass (as built) | second pass (after tuning) |
|---|---:|---:|
| fetched | 8 | 8 |
| have a text layer | 8 | 8 |
| matched a header under its country profile | 0 | not recorded |
| parsed into rows | 0 | 2 |
| reconciled to their own printed opening/closing | 0 | 1 |
| raised `no_rows` (layout not recognised) | — | 6 |

**The clean out-of-sample result is 0 of 8**: the 1-of-8 reconciles only after the parser was tuned on
those same files, so it is in-sample for the added labels.

`results.json` records the **second** pass only; the first pass comes from the build session and is not
in the committed artifact.

In the second pass, the one reconciled sample is a 7-page statement in the IN conventions; it produced
123 rows and its running balance matched its printed opening and closing exactly. The 22-page IN sample
produced 597 rows but did not reconcile, and the remaining six (AU, IN, UK, BR, CO, US) failed
header/layout detection before any transaction row was produced. These are real-world layouts the
synthetic corpus does not model: two-row header blocks, labels outside the profile vocabulary, and
different table geometry.

**Row counts in the local samples could not be independently verified.** There is no hand-built
manifest for them and no second parser to cross-check; the only evidence is reconciliation to each
statement's own printed opening and closing balance. The local numbers are external signal, not a second
gate.

## Run history

The corpus and the parser were built together; the number moved as both were fixed. Every full-corpus run
is recorded, including the ones that failed.

| run | passes | what changed before the run |
|---|---:|---|
| 1 | 0/20 | First full run. `pdfplumber` extraction worked, but the visual-line clustering compared an inverted vertical interval, so no header line was ever assembled; every file raised `no_rows`. |
| 2 | 0/20 | Fixed line clustering. Rows appeared on the AU/SG/US/CO layouts, but every row count was short; IN/BR/CL still raised `no_rows`. |
| 3 | 3/20 | Fixed duplicated normalized tokens (an amount was being read as `3,049.41 3,049.41 3,049.41`). Passed au-02, us-03, co-01; the remaining files were one row short, mis-signed, or (IN) still raised `no_rows`. |
| 4 | 19/20 | Fixed the generator's closing-line baseline overlap (the last transaction and the closing line landed on the same baseline, so the last row was swallowed), and the IN generator's amount branch (IN wrote one amount cell while its header declared Withdrawal/Deposit columns). Only sg-02 failed: the `DR/CR` marker column was not matched. |
| 5 | 20/20 | Normalized `/` in header cells so `DR/CR` matched the marker column. No other change. |
| 6 | 20/20 | General header-cell scoring (exact > suffix > prefix, longer phrase wins) and common label synonyms (`Transaction`, `Particulars`, `Txn Date`, `Ref`, `Details`, `Value Date`) were added after inspecting the local out-of-sample layouts. The first pass over the eight local samples was 0 of 8 rows; the second pass over the same files then gave 2 rows and 1 reconciliation; the synthetic count was unchanged. |

The committed corpus is the one measured in runs 5 and 6. Runs 1–3 used earlier generated bytes; the
generator fixes made before run 4 are the reason the committed PDFs differ from the ones in the first
runs.

## What this does NOT prove

- **Synthetic only.** Every passing statement was generated by the same author who wrote the parser. A
  20/20 on a self-consistent corpus is not evidence of universal bank support.
- **The local signal is weak.** The clean first pass was 0 of 8 rows and 0 of 8 headers; after tuning
  on those same files, 2 produced rows and 1 reconciled, and six were still not recognised as tables.
  Real statements add multi-row headers, bank-specific labels, mixed column geometry and account
  sections that this spike does not handle.
- **A wrong parse is not always an error.** With header labels aligned differently from the values
  beneath them, the seam returned 8 of 14 rows with none of the amounts correct and raised no
  exception. `rows()` never checks its own output against the statement's printed balances; a
  production importer must reconcile and fail loudly instead of trusting a row count.
- **No universal coverage.** No credit-card, investment, business, joint-account or non-English
  statements are in the corpus.
- **No OCR.** A scan raises `no_text_layer`; the importer would need a separate OCR path.
- **One account per file.** A second opening-balance stream raises `multi_account`; split statements
  are refused, not merged.
- **A balance column is required.** Statements without a printed running balance cannot self-check here.
- **No multi-row header blocks.** The local failures include two-row headers; the parser expects the
  header fields on one visual line.
- **No field-mapping UI.** This measures parsing, not the approval, mapping or import flow.
- **No independent row-count oracle.** The synthetic manifest is the oracle for the 20; the local files
  have none.

## Reproduce

With the disposable venv (the one that has pdfplumber and fpdf2), regenerate into a temp directory and
compare with the committed fixtures rather than writing into the tree:

```sh
# Regenerate into a temp dir and compare with the committed fixtures (do not write into the tree).
OUT=$(mktemp -d)
.dispatch/venv/bin/python tests/pdfspike_corpus.py --out "$OUT"
diff -rq --exclude locked.pdf --exclude manifest.json "$OUT" tests/fixtures/pdfspike

# The runner rewrites tests/fixtures/pdfspike/results.json (timings change every run).
.dispatch/venv/bin/python tests/pdfspike_run.py --corpus synthetic
git restore tests/fixtures/pdfspike/results.json

# The seam's own tests need pdfplumber, so they run in this venv too (47 tests).
.dispatch/venv/bin/python -m pytest tests/test_pdfspike_seam.py -x --maxfail=1 -q

# Spike 2's real-layout modes. TUNE may be re-run; HELD-OUT writes a marker and refuses a second run.
.dispatch/venv/bin/python tests/pdfspike_run.py --corpus tune \
    --samples-dir .dispatch/samples/tune --meta .dispatch/samples/tune-meta.json \
    --out .dispatch/tune-results.json
.dispatch/venv/bin/python tests/pdfspike_run.py --corpus heldout \
    --samples-dir <held-out samples dir> --meta <held-out meta.json> \
    --out .dispatch/heldout-results.json
```

The only expected differences in that `diff` are `locked.pdf` (random AES IV) and the manifest entry
that records its sha; everything else reproduces byte-for-byte, and the only extra file in the fixtures
directory is `results.json`, which the runner writes. The eight `x-*.pdf` fixtures are committed and
sha-pinned under `extra_files`; they are outside the 20-file gate. The manifest's `format` note
records that `x-reviewer-left` is the one A4-portrait extra; every other generated PDF is A4
landscape.

In the repository's own venv the test module skips cleanly (no pdfplumber there):

```sh
UV_LINK_MODE=copy uv sync
uv run pytest tests/test_pdfspike_seam.py -x --maxfail=1 -q
```

The local mode requires the deleted local corpus directory and is therefore not
reproducible from this checkout; it was an external signal, not part of the gate.

## Corpus provenance and deletion

- **Committed:** the 20 synthetic PDFs, the four negative controls, `manifest.json` and `results.json`,
  all under `tests/fixtures/pdfspike/`. The generator is `tests/pdfspike_corpus.py`; bank names, people
  and references are invented. The 20 PDFs are deterministic in content, positions and amounts, and
  reproduce byte-for-byte: the generator fixes `CreationDate`, `Producer` and `Creator`, and the runner
  verifies the committed `sha256` before measuring.
- `locked.pdf` uses AES-128, whose IV is random, so its bytes are not reproducible; the manifest records
  the sha of the committed bytes and the runner does not use that control for the gate.
- **Local only, now deleted:** the eight samples (`local-01` .. `local-08`) and their local manifest
  were fetched under the brief's limits, used for the two passes above, and then removed.
  - No sample PDF, page image or extracted statement text is in the repository, and `.dispatch/` is
    git-excluded.
  - The file names of the deleted samples are deliberately not repeated in this document; the deleted
    paths are recorded in the card handoff only.
  - Earlier revisions of this file did quote those paths — this branch is squashed on merge, so those
    revisions do not reach the integration branch.

`.dispatch/` is git-excluded and nothing from it is committed.
