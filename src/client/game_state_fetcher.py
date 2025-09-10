from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen


@dataclass
class GameStateFetcher:
    base_url: str
    storage_path: str | Path = "data/game_states.jsonl"
    timeout: float = 5.0
    etag: Optional[str] = None

    def fetch_state(
        self,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        url = self.base_url
        if params:
            def enc(v: Any) -> str:
                s = json.dumps(v, separators=(",", ":"))
                return s.strip('"') if isinstance(v, (str, int, float, bool)) or v is None else s
            qs = "&".join(f"{k}={enc(v)}" for k, v in params.items())
            url = f"{url}?{qs}"

        req_headers = dict(headers or {})
        if self.etag:
            req_headers["If-None-Match"] = self.etag

        req = Request(url, headers=req_headers)
        with urlopen(req, timeout=self.timeout) as resp:
            payload = resp.read().decode("utf-8")
            self.etag = resp.headers.get("ETag", self.etag)
            data = json.loads(payload)

        if persist:
            path = Path(self.storage_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"timestamp": time.time(), "source_url": url, "etag": self.etag, "state": data}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")

        return data
