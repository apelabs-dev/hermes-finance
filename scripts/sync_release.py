"""Pull only release versions and notes into development; never push or run hooks.

Run from a clean development checkout: python scripts/sync_release.py v0.3.0
The public tag is read in a disposable clone. No export-tool dependency is needed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PUBLIC_URL = 'https://github.com/arspesk/hermes-finance.git'
VERSION_FILES = ('pyproject.toml', 'uv.lock', 'hermes_plugin/plugin.yaml',
                 'src/hermes_finance/__init__.py', '.release-please-manifest.json')
FILES = VERSION_FILES + ('CHANGELOG.md',)
SEMVER = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
PATTERNS = {
    'pyproject.toml': r'(?m)^(version\s*=\s*")[^"]+("[^\n]*)$',
    'uv.lock': r'(?m)(^name = "hermes-finance"\nversion = ")[^"]+("[^\n]*)$',
    'hermes_plugin/plugin.yaml': r'(?m)^(version: )[^\s]+([^\n]*)$',
    'src/hermes_finance/__init__.py': r'(?m)^(__version__ = ")[^"]+("[^\n]*)$',
}


class SyncError(Exception):
    pass


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    result = subprocess.run(['git', *args], cwd=repo, env=env, capture_output=True, text=True)
    if result.returncode:
        raise SyncError(f'git {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def read(repo, rev, path, optional=False):
    if optional and not git(repo, 'ls-tree', rev, '--', path):
        return None
    # Preserve final newlines, unlike git()'s command output.
    result = subprocess.run(['git', 'show', f'{rev}:{path}'], cwd=repo, capture_output=True)
    if result.returncode:
        raise SyncError(f'missing release file: {path}')
    return result.stdout.decode()


def version(path, text):
    if path == '.release-please-manifest.json':
        data = json.loads(text)
        if set(data) != {'.'}:
            raise SyncError('unexpected release manifest keys')
        value = data['.']
    else:
        match = re.search(PATTERNS[path], text)
        if not match:
            raise SyncError(f'missing version: {path}')
        line = match.group(0)
        value = line[len(match[1]):len(line) - len(match[2])]
    if not re.fullmatch(SEMVER, value):
        raise SyncError(f'invalid version: {path}')
    return value


def normalize(path, text):
    """Strip exactly the version field; all other bytes remain significant."""
    version(path, text)
    if path == '.release-please-manifest.json':
        return re.sub(
            r'("\."\s*:\s*")[^"]+(")', r'\1<VERSION>\2', text)
    result, count = re.subn(PATTERNS[path], lambda m: m[1] + '<VERSION>' + m[2], text)
    if count != 1:
        raise SyncError(f'ambiguous version field: {path}')
    return result


def sync(repo: Path, tag: str, public_url: str = PUBLIC_URL) -> bool:
    if not re.fullmatch('v' + SEMVER, tag):
        raise SyncError('tag must be vX.Y.Z (stable numeric version)')
    repo = repo.resolve()
    if git(repo, 'status', '--porcelain', '--untracked-files=normal'):
        raise SyncError('dirty development checkout; commit or remove changes first')
    head = git(repo, 'rev-parse', 'HEAD')
    released_version = tag[1:]
    current = version('pyproject.toml', read(repo, head, 'pyproject.toml'))
    if tuple(map(int, released_version.split('.'))) < tuple(map(int, current.split('.'))):
        raise SyncError('refusing release downgrade')
    with tempfile.TemporaryDirectory(prefix='finance-release-') as td:
        clone = Path(td) / 'public'
        git(Path(td), 'clone', '--quiet', '--no-checkout', '--no-hardlinks', '--', public_url, str(clone))
        release = git(clone, 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}')
        parents = git(clone, 'rev-list', '--parents', '-n', '1', release).split()
        baseline = parents[1] if len(parents) > 1 else release
        # The first parent is the exported tree immediately before the release PR.
        # Comparing it catches accidental non-version edits even when older tags differ greatly.
        updates = {}
        for path in VERSION_FILES:
            old = read(clone, baseline, path, optional=True)
            new = read(clone, release, path, optional=True)
            local = read(repo, head, path, optional=True)
            if new is None and path == '.release-please-manifest.json' and released_version == current:
                continue  # Initial v0.2.0 predates the manifest.
            if new is None or version(path, new) != released_version:
                raise SyncError(f'tag and release version disagree: {path}')
            if old is None:
                if path != '.release-please-manifest.json':
                    raise SyncError(f'missing predecessor file: {path}')
                old = json.dumps({'.': current}, indent=2) + '\n'
            if normalize(path, old) != normalize(path, new):
                raise SyncError(f'non-version public divergence: {path}')
            if local is None or normalize(path, local) != normalize(path, new):
                raise SyncError(f'non-version development divergence: {path}')
            if version(path, local) != current:
                raise SyncError(f'development versions disagree: {path}')
            if local != new:
                updates[path] = new
        old_notes = read(clone, baseline, 'CHANGELOG.md')
        new_notes = read(clone, release, 'CHANGELOG.md')
        local_notes = read(repo, head, 'CHANGELOG.md')
        previous_sections = old_notes.split('\n', 1)[1].lstrip()
        if previous_sections not in new_notes:
            raise SyncError('public CHANGELOG.md removed or edited previous notes')
        if local_notes not in (old_notes, new_notes):
            raise SyncError('development CHANGELOG.md diverges from public predecessor')
        if local_notes != new_notes:
            updates['CHANGELOG.md'] = new_notes
    if not updates:
        return False
    # Recheck after network reads; never overwrite edits made while the clone was fetched.
    if git(repo, 'rev-parse', 'HEAD') != head or git(repo, 'status', '--porcelain', '--untracked-files=normal'):
        raise SyncError('development checkout changed during synchronization')
    try:
        for path, text in updates.items():
            (repo / path).write_text(text)
        git(repo, 'add', '--', *updates)
        git(repo, '-c', 'core.hooksPath=/dev/null', 'commit', '-m', f'chore(release): sync {tag} from public')
    except Exception:
        # Restore only files this operation owns; never reset unrelated development history.
        for path in updates:
            original = read(repo, head, path, optional=True)
            if original is None:
                (repo / path).unlink(missing_ok=True)
            else:
                (repo / path).write_text(original)
        git(repo, 'reset', '-q', head, '--', *updates)
        raise
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('--public-url', default=PUBLIC_URL, help='read-only clone URL (local bare repos work too)')
    args = parser.parse_args()
    try:
        repo = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
        changed = sync(repo, args.tag, args.public_url)
    except (SyncError, ValueError) as error:
        parser.exit(1, f'sync_release: {error}\n')
    print('Release synchronized.' if changed else 'Already synchronized; no commit.')


if __name__ == '__main__':
    main()
