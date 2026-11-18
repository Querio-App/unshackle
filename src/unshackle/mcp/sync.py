"""Timer-based Git refresh into immutable snapshots, retaining the last good version."""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .repository import Repository

log = logging.getLogger(__name__)


class GitRefresh:
    """Each published snapshot is immutable and survives until server shutdown.

    Retaining older snapshots lets requests already reading them finish safely.
    Git hooks, configured filters, submodules and repository scripts are never run.
    """
    def __init__(self, url: str, ref: str = 'main', interval: float = 60,
                 *, cache_dir: Path | None = None, git_timeout: float = 60):
        parsed = urlsplit(url)
        if not url or url.startswith('-') or (parsed.scheme and parsed.scheme not in {'https', 'ssh', 'file'}):
            raise ValueError('Use an HTTPS/SSH Git URL or a local repository path')
        if parsed.password or (parsed.scheme == 'https' and parsed.username) or parsed.query or parsed.fragment:
            raise ValueError('Git credentials must not appear in the repository URL')
        if not ref or ref.startswith('-') or any(c in ref for c in ('\n', '\r', '\0')):
            raise ValueError('Invalid Git ref')
        if not math.isfinite(interval) or interval <= 0 or not math.isfinite(git_timeout) or git_timeout <= 0:
            raise ValueError('Refresh interval and Git timeout must be positive and finite')
        self.url, self.ref, self.interval, self.git_timeout = url, ref, interval, git_timeout
        self._temporary = tempfile.TemporaryDirectory(prefix='unshackle-snapshots-', dir=cache_dir)
        self.directory = Path(self._temporary.name)
        self._current: Path | None = None
        self.revision: str | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def current(self) -> Path:
        with self._lock:
            if self._current is None:
                raise RuntimeError('No validated Git snapshot is available')
            return self._current

    def _git(self, *args: str) -> str:
        # Do not inherit warehouse credentials, GIT_CONFIG_* injection or external
        # filters. SSH may use an explicitly mounted key/agent; no interactive prompts.
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if k in os.environ}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_SSH_COMMAND='ssh -oBatchMode=yes')
        try:
            result = subprocess.run(['git', '-c', f'core.hooksPath={os.devnull}', '-c', 'submodule.recurse=false', *args],
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=self.git_timeout, check=True)
        except (subprocess.SubprocessError, OSError):
            raise RuntimeError('Git operation failed; check repository access and ref') from None
        return result.stdout.strip()

    def refresh(self) -> bool:
        with self._refresh_lock:
            stage = Path(tempfile.mkdtemp(prefix='stage-', dir=self.directory))
            try:
                checkout = stage / 'repo'
                self._git('init', '--template=', '--initial-branch=main', str(checkout))
                self._git('-C', str(checkout), 'fetch', '--depth=1', '--no-tags', '--', self.url, self.ref)
                revision = self._git('-C', str(checkout), 'rev-parse', 'FETCH_HEAD')
                if revision == self.revision:
                    return False
                self._git('-C', str(checkout), 'checkout', '--detach', revision, '--')
                repository = Repository(checkout)
                for path in repository.files():
                    repository.read(path)  # validates notebook JSON and static extraction too
                shutil.rmtree(checkout / '.git')
                snapshot = self.directory / revision
                if not snapshot.exists():
                    checkout.rename(snapshot)
                # Publish one pointer only after checkout and validation finish.
                with self._lock:
                    self._current, self.revision = snapshot, revision
                return True
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError('Refresh timer is already running')
        if self._current is None:
            self.refresh()  # fail startup if there is no last good snapshot
        def poll():
            while not self._stop.wait(self.interval):
                try:
                    self.refresh()
                except Exception:
                    # Never log Git URLs, credentials, stderr or repository contents.
                    log.warning('Git refresh failed; retaining the last good snapshot')
        self._thread = threading.Thread(target=poll, name='unshackle-git-refresh', daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        self._temporary.cleanup()
