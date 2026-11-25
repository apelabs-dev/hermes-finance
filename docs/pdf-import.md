# PDF statements

hermes-finance can import a PDF bank statement as well as CSV, OFX and QFX. PDF import is for **one deposit account
per PDF**, with a text layer and printed balances. It runs entirely on your computer and refuses rather than guesses.

## Install the optional part

PDF reading uses the optional `pdf` extra:

```sh
pip install 'hermes-finance[pdf]'
# or, with uv:
uv pip install 'hermes-finance[pdf]'
```

Without it, CSV, OFX and QFX work as before; a PDF import says the optional part is missing.

## What is supported

- One bank account, with a text layer (not a scan).
- Printed opening and closing balances, and/or a running balance column.
- Common money and date formats: decimal comma, thousands separators, trailing minus, parentheses, `CR` / `DR`
  suffixes, and dates printed with or without a year.
- Statements printed oldest first or newest first; a newest-first statement is replayed from the bottom up.
- A statement with no printed period but with a year you type (or a year printed on every date).

What is not supported: OCR of scanned images, credit-card statements, more than one account (or more than one table of
balances) in one PDF, batch import, and a second writer for imported rows. Download a card statement as CSV, OFX or
QFX instead.

## How an import runs

1. **The local generic parser** reads the PDF's word positions, finds the transaction table, its column roles and the
   printed balances, then replays every row against those balances.
2. **A saved template** for a layout you taught once is tried next, matched by a masked layout fingerprint (the
   header roles and words, page size and the shape of the first lines). No statement text or number is in the
   fingerprint.
3. **An opt-in AI template proposal** is the last step, only when you turn it on for that statement. It sends a
   masked shape of the layout (digits shown as `9`, names shown as `x`/`X`) and gets back column roles and word ids.
   It never sends a number, a description, the raw text layer or the PDF, and the model never returns a number. The
   proposed mapping runs locally and must reconcile before anything is imported. A statement's own column words
   (`Date`, `Debit`, `Balance`) stay readable so the model can name the columns, except a word that also appears
   outside them: a title `Credit Card Statement` masks the `Credit` header, because the mask check refuses any
   statement word anywhere in the prompt and a description can hide a name in one.

Rows reach the preview only when they replay to the statement's printed balances. "Reconcile or refuse" is the rule:
a mismatch imports nothing, even if most rows look right.

## Passwords

A password-protected PDF asks for the password its bank uses for statements. The password is passed to the PDF
reader for that one attempt, in memory. It is not stored, not logged, not written to a temp file and not put in an
error message.

## Teaching a layout once

When a statement imports, you can save its layout as a template (the teach option). The next statement with the same
layout is parsed with the saved mapping without asking again. A template is stored with a masked fingerprint and a
validated config of column positions and rules; it contains no statement text, no description and no number.

A layout whose balances you typed by hand is not saved: next month's statement could not be read without typing them
again, so the commit imports the rows and answers `template: {"saved": false, "reason": "typed_balances"}`. A layout
already saved for these statements is never replaced silently: a preview that sends a different mapping says so in
`pdf.replaces` (`{source, created_at}`), and a teaching commit is refused with the code `template_exists` unless it
sends `replace_template: true`. The commit's `template` answer says whether it saved and whether it replaced.

## Refusal reasons in plain words

The import stops with one specific reason and imports nothing. The main ones:

- The file couldn't be read as a PDF, or needs a password you don't have.
- The PDF is too large or too complex to read safely (`pdf_too_complex`): a file over 10 MB, a statement over 50
  pages, 40,000 words or 2,000,000 characters of text (a single "word" over 200 characters counts as too complex
  too, unless it is a drawn rule or dot leader — those are layout, not data), or one whose read would take more than
  10 seconds or 256 MB. The read runs in a separate process so those limits stop it (a decompression bomb is one
  example); download a shorter date range, or CSV, OFX or QFX instead.
- The PDF is a scan, so there's no text to read.
- It looks like a credit-card statement, or it has more than one account.
- The transaction table wasn't recognized, or it has no transactions.
- The PDF doesn't print balances, so the rows can't be checked.
- The rows don't add up to the printed balances.
- A date or the statement period is missing, or the statement prints no year. The app asks you to type the year;
  it never guesses one.
- A saved layout doesn't fit this statement.
- The optional PDF part isn't installed.

The preview response carries a machine reason code and, when a mapping could fix the problem, the header words and
column candidates it saw. The AI proposal, when one was produced, is offered as a candidate to confirm; it still has
to reconcile. Only a preview asks the AI (`ai_template: true`). To import a proposal, the commit sends the one the
preview returned back as `template` with `template_source: "ai"` (it is saved as an AI template when taught). A
commit with `ai_template: true` is refused with the code `ai_on_commit`, so a commit never asks the AI again. Before
anything is sent, a preview with `ai_view: true` returns `ai_view.prompts`: every prompt the AI tries could send for
this file, word for word, worked out locally with no model call; the app's privacy screen shows them before Send. The
view is the two pages that carry money, and screening it does not grow with the file's word count: the letters are one
intersection with the view's own letter runs, and the numbers go through Aho-Corasick automatons built from the
statement's number words and run over the view, so a statement word holding whitespace is caught wherever it lands and
no word is searched for on its own. One automaton holds at most about 200,000 nodes; the node count is checked after a
word has been added, so it can overshoot by less than one supported word (200 characters), and a word set past that
budget is screened by the next automaton over the same view - at most 11 traversals of one view, which is what the
2,000,000-character read cap allows. A file whose view would exceed 512,000 characters is refused as
`pdf_too_complex` instead of screened and sent; that is the only refusal the screen itself can add, and it is about
the input's size, not the work the guard would do.

## Currency

Imported amounts go into the target manual account, and that account's currency applies. A currency the statement
prints (an ISO code such as `EUR`) is shown in the preview for information only; it is never converted and never
overrides the account.

## What the measurements show

On 11 public sample statements, the local generic parser imported 7 correctly, and the opt-in AI proposal brought the
total to 9, with 0 wrong imports. One of the nine prints no year, so that measurement supplies the year the app asks
the user for. Four layouts were proven only in the sample set, so this is evidence that the approach works, not a
guarantee for any particular bank or layout: a statement that doesn't reconcile is refused rather than guessed, and a
new bank's layout may need one teach-once mapping (or the AI proposal).
