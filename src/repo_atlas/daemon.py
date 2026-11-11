"""Loopback-only shared index daemon and authenticated JSON-line client."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any

from repo_atlas.config import load_config
from repo_atlas.indexer import Indexer
from repo_atlas.store import SQLiteStore


def _metadata_path(root: Path) -> Path:
    return load_config(root).db_path.parent / "daemon.json"


def request_daemon(root: Path, operation: str = "index") -> dict[str, Any] | None:
    metadata_path = _metadata_path(root)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        with socket.create_connection((metadata["host"], int(metadata["port"])), timeout=2.0) as connection:
            connection.sendall((json.dumps({"token": metadata["token"], "operation": operation}) + "\n").encode())
            stream = connection.makefile("rb")
            response = json.loads(stream.readline().decode("utf-8"))
            if "error" in response:
                raise RuntimeError(response["error"])
            return response
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def start_daemon(root: Path) -> subprocess.Popen[bytes] | None:
    root = root.resolve()
    existing = request_daemon(root, "status")
    if existing is not None:
        return None
    metadata_path = _metadata_path(root)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata_path.unlink()
    except FileNotFoundError:
        pass
    log_path = metadata_path.with_suffix(".log")
    log_handle = log_path.open("ab")
    kwargs: dict[str, Any] = {"cwd": root, "stdin": subprocess.DEVNULL, "stdout": log_handle, "stderr": log_handle}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, "-m", "repo_atlas.daemon", "--root", str(root)], **kwargs)
    log_handle.close()
    for _ in range(30):
        response = request_daemon(root, "status")
        if response is not None:
            return process
        if process.poll() is not None:
            raise RuntimeError(f"RepoAtlas daemon exited early; inspect {log_path}")
        time.sleep(0.1)
    process.terminate()
    raise TimeoutError("RepoAtlas daemon did not become ready")


def stop_daemon(root: Path) -> bool:
    response = request_daemon(root, "stop")
    if response is None:
        return False
    metadata_path = _metadata_path(root)
    for _ in range(100):
        if not metadata_path.exists():
            return True
        time.sleep(0.05)
    raise TimeoutError("RepoAtlas daemon acknowledged stop but did not exit")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_daemon(root: Path) -> None:
    root = root.resolve()
    config = load_config(root)
    store = SQLiteStore(config.db_path)
    indexer = Indexer(config, store)
    token = secrets.token_urlsafe(32)
    stop_event = threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                request = json.loads(self.rfile.readline().decode("utf-8"))
                if not secrets.compare_digest(str(request.get("token", "")), token):
                    self.wfile.write(b'{"error":"unauthorized"}\n')
                    return
                operation = request.get("operation")
                if operation == "status":
                    response: dict[str, Any] = {"status": store.status(), "pid": os.getpid()}
                elif operation == "index":
                    response = {"status": indexer.index()}
                elif operation == "stop":
                    response = {"stopping": True}
                    stop_event.set()
                else:
                    response = {"error": "unsupported operation"}
                self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.wfile.write((json.dumps({"error": str(error)}) + "\n").encode("utf-8"))

    server = _Server(("127.0.0.1", 0), Handler)
    metadata_path = _metadata_path(root)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps({
        "host": server.server_address[0], "port": server.server_address[1],
        "token": token, "pid": os.getpid(),
    }), encoding="utf-8")
    if os.name != "nt":
        metadata_path.chmod(0o600)
    server.timeout = 0.25
    next_reconcile = 0.0
    try:
        while not stop_event.is_set():
            server.handle_request()
            if time.monotonic() >= next_reconcile and not stop_event.is_set():
                indexer.index()
                next_reconcile = time.monotonic() + 1.0
    finally:
        server.server_close()
        metadata_path.unlink(missing_ok=True)
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    run_daemon(args.root)


if __name__ == "__main__":
    main()
