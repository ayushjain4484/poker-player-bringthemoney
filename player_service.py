import time
import cgi
import json
import urllib.parse
import http.server
import os
from src.engine.player import Player
from src.strategy.strategy_manager import StrategyManager

HOST_NAME = '0.0.0.0'
PORT_NUMBER = ('PORT' in os.environ and int(os.environ['PORT'])) or 9000

# Initialize StrategyManager once and inject its strategy into Player
try:
    _overrides = json.loads(os.getenv('STRATEGY_OVERRIDES', '{}'))
except Exception:
    _overrides = {}
_MANAGER = StrategyManager(overrides=_overrides)
_PLAYER = Player(strategy=_MANAGER.get_strategy())



class PlayerService(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        # Parse content-type and body safely
        ctype, pdict = cgi.parse_header(self.headers.get('content-type', ''))
        if ctype == 'multipart/form-data':
            postvars = cgi.parse_multipart(self.rfile, pdict)
        elif ctype == 'application/x-www-form-urlencoded':
            length = int(self.headers.get('content-length', '0') or 0)
            qs = self.rfile.read(length).decode()
            postvars = urllib.parse.parse_qs(qs, keep_blank_values=1)
        else:
            postvars = {}


        # Validate action
        if 'action' not in postvars or not postvars['action']:
            self.send_response(400)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"missing action"}')
            return


        action = postvars['action'][0]

        # Optional game_state
        if 'game_state' in postvars and postvars['game_state']:
            game_state = json.loads(postvars['game_state'][0])
        else:
            game_state = {}

        # Always respond with JSON
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        response = b''
        if action == 'bet_request':
            response = b'%d' % _PLAYER.betRequest(game_state)
        elif action == 'showdown':
            _PLAYER.showdown(game_state)
            response = b'{}'
        elif action == 'version':
            response = Player.VERSION.encode()

        self.wfile.write(response)


if __name__ == '__main__':
    server_class = http.server.HTTPServer

    collector = None
    try:
        # Optional background state collector controlled by env vars
        if os.getenv('COLLECT_STATES', '').lower() in ('1', 'true', 'yes'):
            try:
                import ssl
                from urllib.request import urlopen, Request
                from urllib.error import URLError, HTTPError
                from src.client.game_state_fetcher import GameStateFetcher
                from src.services.state_collector import StateCollector

                disable_verify = os.getenv('DISABLE_SSL_VERIFY', '').lower() in ('1', 'true', 'yes')
                ssl_context = None if not disable_verify else ssl._create_unverified_context()


                def _resolve_game_id(tournament_id: str, base: str) -> str:
                    """
                    Resolve the current game id from the tournament's /game endpoint.
                    Supports both plain text and JSON responses.
                    """
                    url = f"{base}/api/tournament/{tournament_id}/game"
                    req = Request(url, headers={"Accept": "application/json,*/*;q=0.8"})
                    with urlopen(req, timeout=5.0, context=ssl_context) as resp:
                        payload = resp.read().decode("utf-8").strip()
                        # Try to parse JSON first
                        try:
                            data = json.loads(payload)
                            # Accept common shapes
                            if isinstance(data, str):
                                return data
                            if isinstance(data, dict):
                                for k in ("game_id", "id", "gameId"):
                                    if k in data and isinstance(data[k], str):
                                        return data[k]
                            # Fallback if array
                            if isinstance(data, list) and data:
                                # take last or first string-ish entry
                                for item in reversed(data):
                                    if isinstance(item, str):
                                        return item
                                    if isinstance(item, dict):
                                        for k in ("game_id", "id", "gameId"):
                                            if k in item and isinstance(item[k], str):
                                                return item[k]
                        except Exception:
                            # Not JSON, maybe plain id in body
                            pass
                        # If not JSON, use raw payload if it looks like an id
                        if payload and len(payload) >= 8 and all(ch.isalnum() for ch in payload):
                            return payload
                        raise RuntimeError(f"Unable to resolve game id from response: {payload[:120]}")


                # Prefer explicit GAME_STATE_URL; otherwise construct from tournament and game
                base_url = os.getenv('GAME_STATE_URL')
                if not base_url:
                    base_host = os.getenv('LEANPOKER_BASE', 'https://live.leanpoker.org').rstrip('/')
                    tournament_id = os.getenv('TOURNAMENT_ID') or '68bf3f775bca7800025c408e'
                    game_id = os.getenv('GAME_ID')

                    if not game_id:
                        try:
                            game_id = _resolve_game_id(tournament_id, base_host)
                            print((time.asctime(), f"Resolved game_id={game_id} for tournament_id={tournament_id}"))
                        except (URLError, HTTPError, RuntimeError) as e:
                            raise RuntimeError(f"COLLECT_STATES enabled but could not resolve GAME_ID: {e}")

                    base_url = f"{base_host}/api/tournament/{tournament_id}/game/{game_id}/log"

                fetcher = GameStateFetcher(
                    base_url=base_url,
                    storage_path=os.getenv('COLLECT_OUT', 'data/game_states.jsonl'),
                    verify_ssl=not disable_verify,
                )
                interval = float(os.getenv('COLLECT_INTERVAL', '2.0'))
                collector = StateCollector(fetcher=fetcher, interval_sec=interval)
                collector.start()
                print((time.asctime(), f"StateCollector started - url={base_url} interval={interval}s (verify_ssl={not disable_verify})"))
            except Exception as e:
                print((time.asctime(), f"StateCollector failed to start: {e}"))

        httpd = server_class((HOST_NAME, PORT_NUMBER), PlayerService)
        print((time.asctime(), "Server Starts - %s:%s" % (HOST_NAME, PORT_NUMBER)))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        httpd.server_close()
        print((time.asctime(), "Server Stops - %s:%s" % (HOST_NAME, PORT_NUMBER)))
    finally:
        if collector:
            try:
                collector.stop()
                print((time.asctime(), "StateCollector stopped"))
            except Exception:
                pass
