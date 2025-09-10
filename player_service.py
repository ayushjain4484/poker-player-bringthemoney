import time
import cgi
import json
import urllib.parse
import http.server
import os
from src.engine.player import Player


HOST_NAME = '0.0.0.0'
PORT_NUMBER = ('PORT' in os.environ and int(os.environ['PORT'])) or 9000


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
            response = b'%d' % Player().betRequest(game_state)
        elif action == 'showdown':
            Player().showdown(game_state)
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
                from src.client.game_state_fetcher import GameStateFetcher
                from src.services.state_collector import StateCollector

                # Prefer explicit GAME_STATE_URL; otherwise build from GAME_ID using the provided tournament URL
                base_url = os.getenv('GAME_STATE_URL')
                if not base_url:
                    game_id = os.getenv('GAME_ID')
                    if not game_id:
                        raise RuntimeError("COLLECT_STATES is enabled but neither GAME_STATE_URL nor GAME_ID is set.")
                    base_url = f"https://live.leanpoker.org/api/tournament/68bf3f775bca7800025c408e/game/{game_id}/log"

                fetcher = GameStateFetcher(
                    base_url=base_url,
                    storage_path=os.getenv('COLLECT_OUT', 'data/game_states.jsonl'),
                )
                interval = float(os.getenv('COLLECT_INTERVAL', '2.0'))
                collector = StateCollector(fetcher=fetcher, interval_sec=interval)
                collector.start()
                print((time.asctime(), f"StateCollector started - url={base_url} interval={interval}s"))
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
