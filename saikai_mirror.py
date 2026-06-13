"""Opt-in, loopback/LAN, read-only web mirror of a running saikai session.

Lives in the application layer. It tees the bytes Textual's driver is already
about to write, so the local console is byte-identical and untouched. No second
App, no second PTY, no daemon outliving the App, no transcript writes. Provider-
neutral terminal code (saikai_terminal.py) gains ZERO network code.
"""
from __future__ import annotations

import queue
from typing import Optional


class MirrorHub:
    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 0,
                 cols: int = 80, rows: int = 24, ingest_cap: int = 256) -> None:
        self._token = token
        self._host = host
        self._port = port
        self._cols = cols
        self._rows = rows
        self._ingest: "queue.Queue[str]" = queue.Queue(ingest_cap)

    def broadcast(self, data: str) -> None:
        """Called from Textual's UI thread (MirrorDriver.write). MUST NOT block.
        Drop the oldest frame when the ingest queue is full."""
        try:
            self._ingest.put_nowait(data)
        except queue.Full:
            try:
                self._ingest.get_nowait()   # drop oldest
            except queue.Empty:
                pass
            try:
                self._ingest.put_nowait(data)
            except queue.Full:
                pass   # never block the UI thread
