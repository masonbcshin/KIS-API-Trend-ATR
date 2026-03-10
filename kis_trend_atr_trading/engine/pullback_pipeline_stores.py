from __future__ import annotations

import queue
import threading
from typing import Dict, List, Optional

try:
    from engine.pullback_pipeline_models import PullbackEntryIntent, PullbackSetupCandidate
except ImportError:
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        PullbackEntryIntent,
        PullbackSetupCandidate,
    )


class ArmedCandidateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._candidates: Dict[str, PullbackSetupCandidate] = {}

    def upsert(self, candidate: PullbackSetupCandidate) -> None:
        with self._lock:
            self._candidates[str(candidate.symbol).zfill(6)] = candidate

    def get(self, symbol: str) -> Optional[PullbackSetupCandidate]:
        with self._lock:
            return self._candidates.get(str(symbol).zfill(6))

    def remove(self, symbol: str) -> Optional[PullbackSetupCandidate]:
        with self._lock:
            return self._candidates.pop(str(symbol).zfill(6), None)

    def size(self) -> int:
        with self._lock:
            return len(self._candidates)

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._candidates.keys())


class DirtySymbolSet:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._symbols: set[str] = set()

    def mark(self, symbol: str) -> None:
        code = str(symbol).zfill(6)
        if not code:
            return
        with self._lock:
            self._symbols.add(code)

    def drain(self, max_items: Optional[int] = None) -> List[str]:
        with self._lock:
            if not self._symbols:
                return []
            symbols = sorted(self._symbols)
            if max_items is not None:
                drained = symbols[: max(int(max_items), 0)]
            else:
                drained = symbols
            for symbol in drained:
                self._symbols.discard(symbol)
            return drained

    def size(self) -> int:
        with self._lock:
            return len(self._symbols)


class EntryIntentQueue:
    def __init__(self, maxsize: int = 256) -> None:
        self._queue: queue.Queue[PullbackEntryIntent] = queue.Queue(maxsize=max(int(maxsize), 1))
        self._lock = threading.Lock()
        self._active_keys: set[str] = set()

    def put_if_absent(self, intent: PullbackEntryIntent) -> bool:
        key = intent.intent_key
        with self._lock:
            if key in self._active_keys:
                return False
            self._active_keys.add(key)
        try:
            self._queue.put_nowait(intent)
            return True
        except queue.Full:
            with self._lock:
                self._active_keys.discard(key)
            return False

    def get(self, timeout: float = 0.5) -> PullbackEntryIntent:
        return self._queue.get(timeout=timeout)

    def complete(self, intent: PullbackEntryIntent) -> None:
        with self._lock:
            self._active_keys.discard(intent.intent_key)
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()
