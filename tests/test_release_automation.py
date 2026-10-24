"""Public release files and safe, local-only development synchronization."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ('pyproject.toml', 'uv.lock', 'hermes_plugin/plugin.yaml', 'src/hermes_finance/__init__.py',
         '.release-please-manifest.json', 'CHANGELOG.md')
ENV = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_COMMITTER_NAME='Test',
           GIT_AUTHOR_EMAIL='test@example.com', GIT_COMMITTER_EMAIL='test@example.com')


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


def module():
    spec = importlib.util.spec_from_file_location('sync_release', ROOT / 'scripts/sync_release.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture(tmp_path, legacy_manifest=False):
    public = tmp_path / 'public'
    public.mkdir()
    git(public, 'init', '-q')
    for name in FILES:
        if legacy_manifest and name == '.release-please-manifest.json':
            continue
        target = public / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (ROOT / name).read_text()
        if name != 'CHANGELOG.md':
            data = data.replace(module().version(name, data), '0.2.0')
        target.write_text(data)
    git(public, 'add', '.')
    git(public, 'commit', '-qm', 'feat: baseline')
    git(public, 'tag', 'v0.2.0')
    dev = tmp_path / 'dev'
    git(tmp_path, 'clone', '-q', str(public), str(dev))
    # sync_release strips GIT_* env, so CI runners without a global identity need a repo-local one.
    git(dev, 'config', 'user.name', 'Test')
    git(dev, 'config', 'user.email', 'test@example.com')
    if legacy_manifest:
        (dev / '.release-please-manifest.json').write_text(json.dumps({'.': '0.2.0'}, indent=2) + '\n')
        git(dev, 'add', '.')
        git(dev, 'commit', '-qm', 'ci: bootstrap manifest')
    # A local bare remote exercises exactly the clone/fetch path without network.
    bare = tmp_path / 'public.git'
    git(tmp_path, 'clone', '-q', '--bare', str(public), str(bare))
    for name in FILES:
        target = public / name
        if not target.exists():
            target.write_text(json.dumps({'.': '0.2.0'}, indent=2) + '\n')
        target.write_text(target.read_text().replace('0.2.0', '0.3.0') if name != 'CHANGELOG.md'
                          else '# Changelog\n\n## 0.3.0\n\nFeatures.\n\n' + target.read_text().split('\n', 1)[1].lstrip())
    git(public, 'add', '.')
    git(public, 'commit', '-qm', 'chore(main): release 0.3.0')
    git(public, 'tag', 'v0.3.0')
    git(public, 'push', '-q', str(bare), 'HEAD', '--tags')
    return dev, public, bare


@pytest.mark.parametrize('legacy_manifest', [False, True])
def test_sync_idempotent_and_version_only(tmp_path, legacy_manifest):
    dev, public, bare = fixture(tmp_path, legacy_manifest)
    mod = module()
    assert mod.sync(dev, 'v0.3.0', str(bare)) is True
    assert git(dev, 'log', '-1', '--format=%s') == 'chore(release): sync v0.3.0 from public'
    for name in FILES:
        assert (dev / name).read_bytes() == (public / name).read_bytes()
    head = git(dev, 'rev-parse', 'HEAD')
    assert mod.sync(dev, 'v0.3.0', str(bare)) is False
    assert git(dev, 'rev-parse', 'HEAD') == head
    with pytest.raises(mod.SyncError, match='downgrade'):
        mod.sync(dev, 'v0.2.0', str(bare))


@pytest.mark.parametrize('change', ['dirty', 'dev', 'public', 'changelog'])
def test_sync_refuses_divergence_without_modifying_dev(tmp_path, change):
    dev, public, bare = fixture(tmp_path)
    target = dev / 'pyproject.toml'
    if change in ('dirty', 'dev'):
        target.write_text(target.read_text() + '\n# concurrent dependency edit\n')
        if change == 'dev':
            git(dev, 'commit', '-qam', 'fix: dependency')
    elif change == 'changelog':
        (dev / 'CHANGELOG.md').write_text('# independent notes\n')
        git(dev, 'commit', '-qam', 'docs: notes')
    else:
        git(public, 'tag', '-d', 'v0.3.0')
        (public / 'uv.lock').write_text((public / 'uv.lock').read_text() + '\n# unrelated\n')
        git(public, 'commit', '-qam', 'fix: unrelated')
        git(public, 'tag', 'v0.3.0')
        git(public, 'push', '-q', '--force', str(bare), 'refs/tags/v0.3.0')
    before = {name: (dev / name).read_bytes() for name in FILES}
    head = git(dev, 'rev-parse', 'HEAD')
    mod = module()
    with pytest.raises(mod.SyncError):
        mod.sync(dev, 'v0.3.0', str(bare))
    assert before == {name: (dev / name).read_bytes() for name in FILES}
    assert git(dev, 'rev-parse', 'HEAD') == head


@pytest.mark.parametrize('tag', ['--upload-pack=oops', 'v0.3.0\nfoo', 'v0.3', '../v0.3.0'])
def test_sync_tag_validation(tag, tmp_path):
    mod = module()
    with pytest.raises(mod.SyncError, match='tag'):
        mod.sync(tmp_path, tag, 'unused')


def test_release_config_and_all_version_updaters():
    config = json.loads((ROOT / 'release-please-config.json').read_text())
    assert config['release-type'] == 'python'
    assert config['include-component-in-tag'] is False
    assert config['include-v-in-tag'] is True
    assert config['bootstrap-sha'] == config['last-release-sha'] == '5e002326164d5bea37379dace78b436ca894dd3a'
    assert config['bump-minor-pre-major'] and not config['bump-patch-for-minor-pre-major']
    assert not config['prerelease']
    sections = {s['type']: s for s in config['changelog-sections']}
    assert {t for t, s in sections.items() if not s.get('hidden')} == {'feat', 'fix'}
    assert all(sections[t]['hidden'] for t in ('docs', 'chore', 'ci', 'style', 'refactor', 'perf', 'test', 'build', 'revert', 'deps'))
    extra = config['packages']['.']['extra-files']
    assert {x['path'] for x in extra} == set(FILES[:4])
    current = module().version('pyproject.toml', (ROOT / 'pyproject.toml').read_text())
    for entry in extra:
        assert entry['type'] == 'generic'
        data = (ROOT / entry['path']).read_text()
        lines = [line for line in data.splitlines() if 'x-release-please-version' in line]
        assert len(lines) == 1
        assert current in lines[0]
        # Generic updater replaces only the annotated line; dependencies must remain byte-identical.
        updated = '\n'.join(line.replace(current, '9.9.9') if 'x-release-please-version' in line else line
                            for line in data.split('\n'))
        assert module().normalize(entry['path'], data) == module().normalize(entry['path'], updated)
    assert json.loads((ROOT / FILES[4]).read_text()) == {'.': current}
    workflow = (ROOT / '.github/workflows/release-please.yml').read_text()
    assert re.search(r'googleapis/release-please-action@[0-9a-f]{40}', workflow)
    assert 'secrets.GITHUB_TOKEN' in workflow and 'secrets.RELEASE_PLEASE_TOKEN' in workflow


def test_readme_pngs_and_capture_dependencies():
    readme = (ROOT / 'README.md').read_text()
    assert '<picture>' in readme and 'prefers-color-scheme: dark' in readme
    for theme in ('light', 'dark'):
        path = f'docs/assets/screenshots/overview-{theme}.png'
        assert path in readme
        data = (ROOT / path).read_bytes()
        assert data[:8] == b'\x89PNG\r\n\x1a\n'
        assert struct.unpack('>II', data[16:24]) == (1440, 1500)
    for path in ('scripts/capture_readme_shots.py', 'scripts/sync_release.py', 'tests/cdp.py',
                 'tests/demo_data.py', 'tests/gate_dbs.py', 'scripts/check.py', 'docs/releasing.md'):
        assert (ROOT / path).exists()


def test_native_readme_capture_framing():
    source = (ROOT / 'scripts/capture_readme_shots.py').read_text()
    assert 'browser.size(1440, 1500, scale=1)' in source
    assert "'css_width': 1440, 'css_height': 1500, 'device_scale': 1" in source
    assert 'width="1440" height="1500"' in (ROOT / 'README.md').read_text()
