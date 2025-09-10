import threading
from typing import Optional, Dict, Any, Callable

from src.client.game_state_fetcher import GameStateFetcher


class StateCollector:
    def __init__(
        self,
        fetcher: GameStateFetcher,
        interval_sec: float = 2.0,
        params_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        headers_provider: Optional[Callable[[], Optional[Dict[str, str]]]] = None,
    ):
        self._fetcher = fetcher
        self._interval = interval_sec
        self._params_provider = params_provider
        self._headers_provider = headers_provider
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval * 2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                params = self._params_provider() if self._params_provider else None
                headers = self._headers_provider() if self._headers_provider else None
                self._fetcher.fetch_state(params=params, headers=headers, persist=True)
            except Exception:
                pass
            finally:
                self._stop.wait(self._interval)
