BringTheMoney – LeanPoker Python Bot
===================================

A minimal Python player for the LeanPoker platform with a basic, safe betting strategy.

Quick start
-----------

1) Install requirements
- Python 3.8+
- pip install -r requirements.txt

2) Run the local player service
- python player_service.py
- The service listens on http://0.0.0.0:9000 by default (or $PORT if set).

3) Test the endpoints
- Version:
  curl -s -X POST \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -d 'action=version' \
    http://localhost:9000

- Bet request (sample game state):
  curl -s -X POST \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode 'action=bet_request' \
    --data-urlencode 'game_state={
      "tournament_id":"550d1d68cd7bd10003000003",
      "game_id":"550da1cb2d909006e90004b1",
      "round":0,
      "bet_index":0,
      "small_blind":10,
      "current_buy_in":20,
      "minimum_raise":20,
      "pot":40,
      "dealer":1,
      "orbits":0,
      "in_action":0,
      "players":[
        {"id":0,"name":"BringTheMoney","status":"active","version":"local","stack":1000,"bet":0,
         "hole_cards":[{"rank":"Q","suit":"hearts"},{"rank":"Q","suit":"spades"}]},
        {"id":1,"name":"Opponent","status":"active","stack":1000,"bet":20}
      ],
      "community_cards":[]
    }' \
    http://localhost:9000

Deploying to LeanPoker
----------------------
This repo already contains Procfile, runtime.txt and requirements.txt suitable for Heroku-style deployment (used by LeanPoker).
- Ensure your repository is connected to the LeanPoker tournament as instructed by organizers.
- The app must respond to POST with actions: version, bet_request, and showdown at the root path.

Strategy overview (v0.1)
------------------------
- Pocket pairs: raise the minimum over the call.
- Two high cards (Q or better) or pairing the board: call; add a tiny raise if cheap.
- Otherwise: fold unless calling is very cheap (~2% of stack).
- Always caps bet to available stack and returns a non-negative integer.

Where to edit the bot
---------------------
- player.py → contains the Player class and the betRequest logic you can improve.
- player_service.py → minimal HTTP server LeanPoker expects.

More info
---------
- LeanPoker docs: http://leanpoker.org
- Game state format: https://github.com/lean-poker/poker-spec
