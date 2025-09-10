import argparse
import json
import os
import sys
import time
from typing import Optional, Dict
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import ssl
import gzip
import zlib

from src.client.game_state_fetcher import GameStateFetcher


def _cookie_header_from_sources(arg_cookie: Optional[str], cookie_file: Optional[str], env_cookie: Optional[str]) -> Optional[str]:
    """
    Build a Cookie header string. Accepts:
    - direct string like "name=value; name2=value2"
    - a file path with lines "name<TAB>value" or "name=value" or raw "name=value; ...".
    - environment variable COOKIES
    """
    def normalize(s: str) -> str:
        s = s.strip()
        if not s:
            return ""
        if ";" in s and "=" in s:
            return "; ".join(part.strip() for part in s.split(";") if part.strip())
        if "=" in s:
            return s
        return ""

    if arg_cookie:
        return normalize(arg_cookie)

    if cookie_file:
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            pairs = []
            for ln in lines:
                if "\t" in ln:
                    name, value = ln.split("\t", 1)
                    pairs.append(f"{name.strip()}={value.strip()}")
                elif "=" in ln:
                    pairs.append(ln)
            if pairs:
                return "; ".join(pairs)
        except Exception:
            pass

    if env_cookie:
        return normalize(env_cookie)

    return None


def _common_headers(cookie_header: Optional[str]) -> Dict[str, str]:
    h = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "LeanPokerFetcher/1.0 (+python-urllib)",
        "Connection": "close",
        "Accept-Encoding": "gzip, deflate",
    }
    if cookie_header:
        h["Cookie"] = cookie_header
    return h


def _is_cert_error(err: Exception) -> bool:
    msg = str(err) or ""
    return "CERTIFICATE_VERIFY_FAILED" in msg or isinstance(err, ssl.SSLError)


def resolve_game_id(
    tournament_id: str,
    base: str,
    timeout: float = 5.0,
    insecure: bool = False,
    headers: Optional[Dict[str, str]] = None,
    allow_http_fallback: bool = True,
) -> str:
    """
    Resolve the current game id from the tournament's /game endpoint.
    - Retries with unverified SSL if a certificate error is detected.
    - Optionally falls back to http:// if https:// fails due to SSL.
    - Handles gzip/deflate responses.
    """
    base = base.rstrip("/")
    url_https = f"{base}/api/tournament/{tournament_id}/game"
    context = None if not insecure else ssl._create_unverified_context()

    def _read_text(resp) -> str:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        # Decompress per header or magic bytes
        if enc == "gzip" or (len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B):
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                raw = zlib.decompress(raw)
        return raw.decode("utf-8", errors="strict").strip()

    def _fetch(u: str, ctx) -> str:
        req = Request(u, headers=headers or {})
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            return _read_text(resp)

    # First attempt (honor requested security mode)
    try:
        payload = _fetch(url_https, context)
    except Exception as e:
        # If an SSL issue and we haven't tried insecure yet, retry insecure
        if _is_cert_error(e) and context is None:
            try:
                payload = _fetch(url_https, ssl._create_unverified_context())
            except Exception as e2:
                # If still SSL issue, optionally try http fallback
                if _is_cert_error(e2) and allow_http_fallback and url_https.startswith("https://"):
                    try:
                        url_http = url_https.replace("https://", "http://", 1)
                        payload = _fetch(url_http, None)
                    except Exception as e3:
                        raise e3
                else:
                    raise e2
        else:
            # Non-SSL error or already insecure attempted
            raise e

    # Try JSON shapes
    try:
        data = json.loads(payload)
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for k in ("game_id", "id", "gameId"):
                if k in data and isinstance(data[k], str):
                    return data[k]
        if isinstance(data, list) and data:
            for item in reversed(data):
                if isinstance(item, str):
                    return item
                if isinstance(item, dict):
                    for k in ("game_id", "id", "gameId"):
                        if k in item and isinstance(item[k], str):
                            return item[k]
    except Exception:
        pass

    # Fallback: plain id in body
    if payload and len(payload) >= 8 and all(ch.isalnum() for ch in payload):
        return payload
    raise RuntimeError(f"Unable to resolve game id from response: {payload[:200]}")


def build_log_url(base: str, tournament_id: str, game_id: str) -> str:
    return f"{base.rstrip('/')}/api/tournament/{tournament_id}/game/{game_id}/log"


def summarize(payload: dict) -> str:
    t = payload.get("type") or payload.get("event") or "?"
    msg = payload.get("message")
    if isinstance(msg, str):
        return f"{t}: {msg}"
    if "game_state" in payload:
        gs = payload["game_state"] or {}
        gid = gs.get("game_id") or "?"
        rnd = gs.get("round")
        return f"{t} (game_id={gid}, round={rnd})"
    return t


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Fetch LeanPoker game logs (once or continuously).")
    p.add_argument("--url", help="Full log URL to fetch (overrides other options).")
    p.add_argument("--base", default=os.getenv("LEANPOKER_BASE", "https://live.leanpoker.org"),
                   help="Base host (default: https://live.leanpoker.org)")
    p.add_argument("--tournament-id", default=os.getenv("TOURNAMENT_ID", "68bf3f775bca7800025c408e"),
                   help="Tournament ID")
    p.add_argument("--game-id", default=os.getenv("GAME_ID"),
                   help="Game ID (if omitted, will resolve from /game)")
    p.add_argument("--out", default=os.getenv("COLLECT_OUT", "data/game_states.jsonl"),
                   help="Path to JSONL file to append fetched states")
    p.add_argument("--interval", type=float, default=float(os.getenv("COLLECT_INTERVAL", "2.0")),
                   help="Polling interval in seconds (when following)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="HTTP timeout in seconds")
    p.add_argument("--once", action="store_true",
                   help="Fetch only once and exit")
    p.add_argument("--insecure", action="store_true",
                   help="Disable SSL certificate verification for requests (also auto-retried on SSL errors)")
    p.add_argument("--cookie", help="Cookie header value, e.g. 'rack.session=...; _ga=...; _gid=...'")
    p.add_argument("--cookie-file", help="Path to file with cookies (lines with 'name<TAB>value' or 'name=value')")
    args = p.parse_args(argv)

    # Build headers (include cookies if provided)
    cookie_env = os.getenv("COOKIES")
    cookie_header = _cookie_header_from_sources(args.cookie, args.cookie_file, cookie_env)
    headers = _common_headers(cookie_header)

    insecure_env = os.getenv("DISABLE_SSL_VERIFY", "").lower() in ("1", "true", "yes")
    insecure = args.insecure or insecure_env

    # If insecure, disable verification globally for urllib
    if insecure:
        ssl._create_default_https_context = ssl._create_unverified_context
        print("[warn] SSL verification disabled (global).")

    if cookie_header:
        print("[info] Using Cookie header.")

    # Determine URL
    if args.url:
        url = args.url
        print(f"[info] Using explicit URL: {url}")
    else:
        tid = args.tournament_id
        gid = args.game_id
        if not gid:
            try:
                gid = resolve_game_id(
                    tid,
                    args.base,
                    timeout=args.timeout,
                    insecure=insecure,
                    headers=headers,
                    allow_http_fallback=True,
                )
                print(f"[info] Resolved game_id={gid} for tournament_id={tid}")
            except (URLError, HTTPError, RuntimeError) as e:
                print(f"[error] Failed to resolve game id: {e}")
                return 2
        url = build_log_url(args.base, tid, gid)
        print(f"[info] Using log URL: {url}")

    # Primary fetcher (https or as provided)
    fetcher = GameStateFetcher(
        base_url=url,
        storage_path=args.out,
        timeout=args.timeout,
        verify_ssl=not insecure,
        default_headers=headers,
    )

    def do_fetch_once() -> bool:
        try:
            data = fetcher.fetch_state(persist=True)
            if isinstance(data, dict):
                print(f"[ok] {summarize(data)}")
            elif isinstance(data, list) and data:
                last = data[-3:]
                print("[ok] batch:")
                for ev in last:
                    if isinstance(ev, dict):
                        print("  -", summarize(ev))
                    else:
                        print("  -", str(ev)[:140])
            else:
                print("[ok] received (non-dict) payload")
            return True
        except URLError as e:
            # If SSL error on https, attempt http fallback once
            if _is_cert_error(e) and fetcher.base_url.startswith("https://"):
                fallback_url = fetcher.base_url.replace("https://", "http://", 1)
                print(f"[warn] SSL error on https; retrying via http: {fallback_url}")
                try:
                    temp = GameStateFetcher(
                        base_url=fallback_url,
                        storage_path=fetcher.storage_path,
                        timeout=fetcher.timeout,
                        verify_ssl=False,
                        default_headers=headers,
                    )
                    data = temp.fetch_state(persist=True)
                    if isinstance(data, dict):
                        print(f"[ok] {summarize(data)}")
                    elif isinstance(data, list) and data:
                        last = data[-3:]
                        print("[ok] batch (http fallback):")
                        for ev in last:
                            if isinstance(ev, dict):
                                print("  -", summarize(ev))
                            else:
                                print("  -", str(ev)[:140])
                    else:
                        print("[ok] received (non-dict) payload (http fallback)")
                    return True
                except Exception as e2:
                    print(f"[error] URL error after http fallback: {e2}")
                    return False
            print(f"[error] URL error: {e}")
            return False
        except HTTPError as e:
            if e.code == 304:
                print("[ok] 304 Not Modified")
                return True
            print(f"[error] HTTP {e.code}: {e.reason}")
            return False
        except Exception as e:
            print(f"[error] Unexpected: {e}")
            return False

    if args.once:
        ok = do_fetch_once()
        return 0 if ok else 1

    print(f"[info] Following with interval={args.interval}s. Press Ctrl+C to stop.")
    try:
        while True:
            do_fetch_once()
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n[info] Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())