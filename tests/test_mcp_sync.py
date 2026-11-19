import subprocess
import threading
import time

import pytest

from unshackle.mcp.sync import GitRefresh
from unshackle.mcp.repository import Repository


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'origin'
    repo.mkdir()
    git(repo, 'init', '--initial-branch=main')
    git(repo, 'config', 'user.email', 'fixture@example.com')
    git(repo, 'config', 'user.name', 'Synthetic fixture')
    (repo / 'knowledge').mkdir()
    (repo / 'knowledge/note.md').write_text('first')
    commit(repo)
    return repo


def commit(repo):
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'Synthetic snapshot')


def test_refresh_atomic_last_good_and_pinned_reads(source):
    refresh = GitRefresh(str(source))
    try:
        assert refresh.refresh()
        first = refresh.current()
        assert not (first / '.git').exists()
        assert not refresh.refresh()
        (source / 'knowledge/note.md').write_text('second')
        commit(source)
        assert refresh.refresh()
        assert Repository(refresh.current()).read('knowledge/note.md')['content'] == 'second'
        assert Repository(first).read('knowledge/note.md')['content'] == 'first'
        good = refresh.current()
        (source / 'unshackle.toml').write_text('[paths]\nknowledge="../escape"')
        commit(source)
        with pytest.raises(ValueError):
            refresh.refresh()
        assert refresh.current() == good
        refresh.url = str(source / 'missing')
        with pytest.raises(RuntimeError, match='Git operation failed'):
            refresh.refresh()
        assert refresh.current() == good
    finally:
        refresh.close()


def test_timer_and_concurrent_readers(source):
    refresh = GitRefresh(str(source), interval=.05)
    errors = []
    stop = threading.Event()
    def reader():
        while not stop.wait(.005):
            try:
                assert Repository(refresh.current()).read('knowledge/note.md')['content'] in {'first', 'second'}
            except Exception as exc:
                errors.append(exc)
    try:
        refresh.start()
        worker = threading.Thread(target=reader)
        worker.start()
        initial = refresh.revision
        (source / 'knowledge/note.md').write_text('second')
        commit(source)
        deadline = time.monotonic() + 5
        while refresh.revision == initial and time.monotonic() < deadline:
            time.sleep(.02)
        assert refresh.revision != initial
        assert not errors
    finally:
        stop.set()
        worker.join()
        refresh.close()


def test_no_hooks_or_filters_and_pinned_commit(source, tmp_path, monkeypatch):
    marker = tmp_path / 'executed'
    (source / '.gitattributes').write_text('*.md filter=attack\n')
    commit(source)
    revision = git(source, 'rev-parse', 'HEAD')
    # A source hook is not copied; inherited Git filter configuration is ignored.
    hook = source / '.git/hooks/post-checkout'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o755)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'filter.attack.smudge')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', f'touch {marker}')
    refresh = GitRefresh(str(source), ref=revision)
    try:
        refresh.refresh()
        assert refresh.revision == revision
        assert not marker.exists()
    finally:
        refresh.close()


@pytest.mark.parametrize('url', ['https://user:secret@example.com/repo', 'https://user@example.com/repo',
                              'https://example.com/repo?token=secret', 'ext::command', '--upload-pack=bad'])
def test_reject_secret_or_command_urls(url):
    with pytest.raises(ValueError):
        GitRefresh(url)
