from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen
import ssl
import gzip
import zlib

@dataclass
class GameStateFetcher:
    base_url: str
    storage_path: str | Path = "data/game_states.jsonl"
    timeout: float = 5.0
    etag: Optional[str] = None
    verify_ssl: bool = True
    default_headers: Optional[Dict[str, str]] = None

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

        # Merge headers and ensure compression + agent
        req_headers = dict(self.default_headers or {})
        if headers:
            req_headers.update(headers)
        # Be liberal; some endpoints send HTML on auth redirects, still gzip-compressed
        req_headers.setdefault("Accept", "application/json,text/html,text/plain,*/*")
        req_headers.setdefault("Accept-Encoding", "gzip, deflate")
        req_headers.setdefault("User-Agent", "LeanPokerFetcher/1.0 (+python-urllib)")
        if self.etag:
            req_headers["If-None-Match"] = self.etag

        req = Request(url, headers=req_headers)
        context = None if self.verify_ssl else ssl._create_unverified_context()
        with urlopen(req, timeout=self.timeout, context=context) as resp:
            raw = resp.read()
            self.etag = resp.headers.get("ETag", self.etag)

            # Decompress if needed
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            raw = self._maybe_decompress(raw, enc)

            # Decode text; if decoding fails, try decompress paths again heuristically
            try:
                payload = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Last-resort attempts: try gzip then deflate in case header was missing
                try:
                    payload = gzip.decompress(raw).decode("utf-8")
                except Exception:
                    try:
                        payload = zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8")
                    except Exception:
                        payload = raw.decode("utf-8", errors="replace")

        # Parse JSON or raise a useful error
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # If it looks like HTML (likely auth/redirect), show short diagnostics
            snippet = payload[:200].replace("\n", " ").replace("\r", " ")
            raise RuntimeError(f"Unexpected non-JSON response from {url}: {snippet}")

        if persist:
            path = Path(self.storage_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"timestamp": time.time(), "source_url": url, "etag": self.etag, "state": data}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                print(f"Saved game state to {path}")

        return data

    @staticmethod
    def _maybe_decompress(raw: bytes, content_encoding: str) -> bytes:
        try:
            if content_encoding == "gzip" or (len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B):
                return gzip.decompress(raw)
            if content_encoding == "deflate":
                try:
                    return zlib.decompress(raw, -zlib.MAX_WBITS)
                except zlib.error:
                    return zlib.decompress(raw)
        except Exception:
            # Fall back to raw on any decompression issue; caller will re-attempt
            pass
        return raw