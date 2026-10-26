"""Jupyter and marimo servers for generated notebooks, started on first use.

Each runs as a child process of the web app on a free localhost port with a
random token, rooted at the reports directory, and stops when the app stops.
Notebooks reach the database through the app (see `client.py`).
"""
import atexit
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class NotebookServers:
    def __init__(self, directory: Path, app_url: str, database: str | None = None, prepare=None):
        self.directory, self.app_url, self.database = directory, app_url, database
        self.prepare = prepare  # prepare(kind) runs before a server starts (e.g. marimo's settings file)
        self.servers: dict[str, tuple[subprocess.Popen, int, str]] = {}
        self.lock = threading.Lock()
        atexit.register(self.stop)

    def _command(self, kind: str, port: int, token: str) -> list[str]:
        if kind == 'jupyter':
            return [sys.executable, '-m', 'jupyter', 'lab', '--no-browser', '--ServerApp.ip=127.0.0.1',
                    f'--ServerApp.port={port}', f'--ServerApp.token={token}',
                    f'--ServerApp.root_dir={self.directory}', '--ServerApp.port_retries=0']
        return [sys.executable, '-m', 'marimo', 'edit', str(self.directory), '--headless', '--host', '127.0.0.1',
                '--port', str(port), '--token-password', token, '--skip-update-check']

    def _ensure(self, kind: str) -> tuple[int, str]:
        with self.lock:
            running = self.servers.get(kind)
            if running and running[0].poll() is None:
                return running[1], running[2]
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.prepare is not None:
                self.prepare(kind)
            port, token = _free_port(), secrets.token_urlsafe(16)
            log = (self.directory / f'.{kind}.log').open('ab')
            env = {**os.environ, 'NEMBOT_URL': self.app_url, **({'NEMBOT_DATABASE': self.database} if self.database else {})}
            process = subprocess.Popen(self._command(kind, port, token), stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=env, cwd=self.directory)
            self.servers[kind] = (process, port, token)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:  # wait until it answers HTTP
            if process.poll() is not None:
                raise RuntimeError(f'{kind} server exited; see {self.directory / f".{kind}.log"}')
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2)
                break
            except urllib.error.HTTPError:
                break  # it's up (auth or redirect responses are fine)
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(f'{kind} server did not start within 60 s')
        return port, token

    def url(self, kind: str, filename: str) -> str:
        port, token = self._ensure(kind)
        quoted = urllib.parse.quote(filename)
        if kind == 'jupyter':
            return f'http://127.0.0.1:{port}/lab/tree/{quoted}?token={token}'
        return f'http://127.0.0.1:{port}/?file={quoted}&access_token={token}'

    def running(self, kind: str) -> tuple[int, str] | None:
        """(port, token) of a server that's already running, without starting one."""
        server = self.servers.get(kind)
        return (server[1], server[2]) if server and server[0].poll() is None else None

    def stop(self):
        for process, _, _ in list(self.servers.values()):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.servers.clear()
