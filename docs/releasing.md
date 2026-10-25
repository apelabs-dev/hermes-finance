# Releasing

Development history stays private. Public `main` contains allowlisted squash exports
plus Release Please's version/changelog commits. Never push development history to
the distribution repository. `scripts/export_public.py` and its allowlist intentionally
remain development-only; `scripts/sync_release.py` is standalone and also ships publicly.

## Policy and baseline

The existing `v0.2.0` prerelease points at
`2b65046b1221d4cfb983091196ab77590d9ca38a`. Do not recreate it. The manifest starts
at `0.2.0`; both bootstrap and last-release boundaries point to that commit.
Release Please discovers subsequent releases normally; the boundary remains the
oldest commit it needs to search, not a forced version.

Conventional Commit squash titles drive versions: `feat: ...` → next minor,
`fix: ...` → next patch. Before 1.0, breaking changes bump the minor, too.
Explicit `changelog-sections` expose only `feat` and `fix`; ordinary `docs`,
`chore`, `ci`, `perf`, `deps`, `revert` and other hidden types alone do not produce
a release PR. A breaking-change footer remains release-eligible even on a hidden
type. This overrides the Python strategy default, which exposes `docs` and would
release an ordinary documentation-only commit as a patch. Version selection and
release eligibility are separate: hidden ordinary commits have empty release notes,
so Release Please skips the PR despite calculating a candidate patch version.
Use a descriptive title representing the whole export; include deterministic notes
with `--body` (including a `BREAKING CHANGE:` footer when applicable).

New releases use stable numeric `vX.Y.Z` tags, no component prefix, and GitHub's
prerelease flag is false. The product remains beta throughout 0.x; a numeric tag is
not a claim of 1.0 stability. This deliberately leaves the original 0.2.0 prerelease
untouched.

## Export and release loop

1. Finish and commit development changes on the reviewed branch. Run the targeted
   checks and the privacy scan. Capture README images when the Overview changes:
   `PYTHONPATH=src python scripts/capture_readme_shots.py --evidence-dir /path/outside/repo`.
   The capture uses exported fixtures and helpers, a pinned 2026-09-25 date, one
   managed Chrome slot and a temporary synthetic database. The images are
   2880 × 1800 PNGs: a real 1440 × 900 CSS viewport at device scale 2, so they stay
   crisp when GitHub scales them down. The README sets no width/height on them, which
   keeps the aspect ratio on phones. Commit only the two PNGs.
2. The operator clones the distribution repository into a disposable directory and
   exports the committed source, using the development-only tool:

   ```sh
   git clone https://github.com/<owner>/hermes-finance.git "$PUBLIC_CLONE"
   python scripts/export_public.py --into "$PUBLIC_CLONE" --title 'feat: explain the exported changes' --body 'Deterministic release notes.'
   ```

   `--title` is required (except for read-only `--list`), single-line, and must use
   a supported Conventional Commit type. Export dates come from the source commit.
   Same source, title, body and parent produce the same commit. `--into` appends one
   commit to the existing branch; an identical tree produces no commit. It never
   rewrites earlier history. Unsynchronized public versions or changelog notes are
   refused before writing any destination object or file.
3. The orchestrator reviews the export and pushes its branch to public `main`.
   Release Please sees that squash title and opens/updates a release PR. The maintainer
   reviews and merges the PR. That merge causes Release Please to create the next
   tag and GitHub release. No package publishing is configured.
4. After the public release PR merges and its tag exists, run in a **clean development
   checkout**, on the branch that will be exported next:

   ```sh
   python scripts/sync_release.py v0.3.0 --public-url https://github.com/<owner>/hermes-finance.git
   ```

   This fetches the public tag in a temporary clone and commits only
   `pyproject.toml`, the project entry in `uv.lock`, `hermes_plugin/plugin.yaml`,
   `src/hermes_finance/__init__.py`, `.release-please-manifest.json`, and `CHANGELOG.md`,
   with `chore(release): sync v0.3.0 from public`. Repeating it is a no-op. No push
   occurs. Tags must be stable numeric versions; downgrades are refused.
5. Continue development, then export again with a fresh Conventional Commit title.
   The synced manifest prevents recreation of the previous release.

The sync compares a release commit with its first parent (the export immediately
before the release PR), rather than the previous release tag: exports between tags
can contain unrelated changes. Every version file must differ only in its precise
version field. Development non-version differences and independent changelog edits
cause refusal. Existing changelog sections must survive. If a dependency edit races
a release, reconcile it deliberately on a separate branch before retrying; the script
will not overwrite it. The initial 0.2.0 tag predates the manifest and is handled
without requiring that file there. Temporary clones are removed on every exit.

## Version updaters and checks

The Python strategy updates the package and prepends changelog entries, preserving
`## 0.2.0`. Explicit `generic` extra-file updaters mark one line per version file
with `x-release-please-version`. In `uv.lock` only the `hermes-finance` project's
version line is annotated; dependency versions and hashes are untouched. No lock
regeneration or dependency resolution is needed for a release-only bump.
`tests/test_plugin_release.py::test_beta_release_versions_agree` compares package,
lockfile, plugin, `__version__` and MCP `build_server().version` dynamically.

The action is pinned to verified v4.4.1 commit
`5c625bfb5d1ff62eadeeb3772007f7f66fdcf071` (bundles release-please 17.3.0).
The supported annotation format is documented in
[customizing](https://github.com/googleapis/release-please/blob/v17.3.0/docs/customizing.md#updating-arbitrary-files)
and implemented by the [Generic updater](https://github.com/googleapis/release-please/blob/v17.3.0/src/updaters/generic.ts).
The Python strategy also handles `pyproject.toml`; the duplicate assignment is
idempotent. A local exercise of the real 17.3.0 Python strategy and merged
updaters verified that all four files change only their version fields, preserving
annotations, dependencies and formatting; the sync normalization accepts those
outputs. Manifest updates are built in. New release sections use Release Please's
normal `## [X.Y.Z](...) (date)` format; keep the original 0.2.0 section below them.

The existing `ci.yml` runs on ordinary pushes and pull requests: Ubuntu no-browser
checks and the Python 3.11 plugin compile check. The macOS full suite remains nightly.
The release workflow does not duplicate it.

## GitHub setup (operator only)

Default authentication is `GITHUB_TOKEN`, with workflow-scoped `contents: write`
and `pull-requests: write`. In repository Settings → Actions → General → Workflow
permissions enable **Allow GitHub Actions to create and approve pull requests**.
An organization policy may prevent changing it. Operator commands (never part of
export or sync):

```sh
gh api repos/<owner>/hermes-finance/actions/permissions/workflow
gh api --method PUT repos/<owner>/hermes-finance/actions/permissions/workflow -F can_approve_pull_request_reviews=true
```

PRs created using `GITHUB_TOKEN` do not trigger other workflows. To have normal CI
run automatically on the release PR, an operator may configure a fine-grained PAT
for this repository with Contents and Pull requests read/write access, and store it
as `RELEASE_PLEASE_TOKEN`:

```sh
gh secret set RELEASE_PLEASE_TOKEN --repo <owner>/hermes-finance
```

The command prompts securely; do not put a token in a command or log. The workflow
prefers that optional secret, otherwise falls back to `GITHUB_TOKEN`. Without the
PAT, run CI through the existing manual/maintainer process before merging a release
PR (for example, explicitly dispatch a suitably authorized check or test the PR
locally); the default token does not imply PR CI ran. No settings, secrets, pushes,
tags or releases are changed by local implementation tests.
