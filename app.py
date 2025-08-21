# app.py — Ultimate Voice Stats server
# - Vosk + Socket.IO (eventlet)
# - Undo + per-game exports
# - Auto "New Point" after assists
# - Dedupe window = 4 seconds
# - Supports: throw, huck, assist, assist to, score, D, drop
# - Phrases like "Jake turn to Mario" and "Jake to Mario (completed/turn)" supported

import eventlet
eventlet.monkey_patch()

import os, re, csv, json, time, logging
from eventlet.queue import Queue, Empty
from eventlet.semaphore import Semaphore

from flask import Flask, request, render_template, jsonify, send_from_directory
from flask_socketio import SocketIO
from vosk import Model, KaldiRecognizer, SetLogLevel

# ---- Logging ----
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s:%(lineno)d | %(message)s",
)
logger = logging.getLogger("statkeeper")
SetLogLevel(-1)  # quiet Vosk internal logs

# ---- Flask / Socket.IO (Eventlet) ----
app = Flask(__name__, static_folder="static", static_url_path="/static")
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=64 * 1024 * 1024,
    logger=False,
    engineio_logger=False,
)

# ---- Config ----
SAMPLE_RATE = 16000
MODEL_PATH = os.getenv("MODEL_PATH", "/models/vosk")
DATA_DIR   = os.getenv("DATA_DIR", "/data")
AUTOSAVE   = os.getenv("AUTOSAVE", "0")  # "1" to enable periodic autosave

# Roster (multi-word supported)
ROSTER = [
    "Caden","Eli","JP","Rudy","Dylan","Mario","Adam","Pow","Leo","Nate","Shane",
    "Jake","Brandon","Justice","Kale","Martin","Walker","Tucker","Beemer","Big Jake","Christian"
]

# Dedupe window (seconds) for combining voice commands (requested: 4s)
DEDUPE_WINDOW_SEC = 4.0

# ---- Globals ----
_model = None
REC = None
LOCK = Semaphore(1)
AUDIO_Q = Queue(maxsize=256)
LAST_PARTIAL = ""

METRICS = {
    "chunks_enqueued": 0,
    "chunks_dropped": 0,
    "chunks_processed": 0,
    "commands_applied": 0,
    "q_max": 0,
    "last_chunk_ts": 0.0,
    "last_result_ts": 0.0,
    "disconnects": 0,
}

def ts(): return time.strftime("%H:%M:%S")

# ---- Stats model with Undo + dedupe rules + auto new point on assist ----
class StatBook:
    def __init__(self, roster):
        self.roster = [r.strip().lower() for r in roster if r.strip()]
        self._history = []
        self._history_max = 1000
        self.reset()

    def reset(self):
        self.game_no = 1
        self.point_no = 1
        self.events = []  # list of dicts (include ts_epoch for de-dupe)
        self.players = {
            n: {"ds": 0, "assists": 0, "scores": 0,
                "throws": 0, "completions": 0, "turnovers": 0,
                "hucks": 0, "huck_completions": 0,
                "drops": 0}
            for n in self.roster
        }
        self._history = []
        self._last_point_change = 0.0   # guard to avoid double-increment auto+manual

    # --- history helpers ---
    def _snapshot(self):
        return {
            "game_no": self.game_no,
            "point_no": self.point_no,
            "events_len": len(self.events),
            "players": {k: dict(v) for k, v in self.players.items()},
        }

    def _push_history(self):
        self._history.append(self._snapshot())
        if len(self._history) > self._history_max:
            self._history.pop(0)

    def undo(self):
        if not self._history:
            return False
        m = self._history.pop()
        self.game_no = m["game_no"]
        self.point_no = m["point_no"]
        self.players = {k: dict(v) for k, v in m["players"].items()}
        self.events = self.events[:m["events_len"]]
        return True

    # --- small utils ---
    def _blank(self):
        return {"ds": 0, "assists": 0, "scores": 0,
                "throws": 0, "completions": 0, "turnovers": 0,
                "hucks": 0, "huck_completions": 0,
                "drops": 0}

    def _ensure(self, name):
        n = (name or "").lower().strip()
        if not n: return n
        if n not in self.players:
            self.players[n] = self._blank()
        return n

    def _now(self):
        return time.time()

    def add_event(self, ev): self.events.append(ev)

    # Return most recent completed throw/huck by X (optionally to Y) within window
    def _find_recent_completed(self, x, y=None, within=DEDUPE_WINDOW_SEC):
        now = self._now()
        for ev in reversed(self.events):
            if ev.get("game") != self.game_no or ev.get("point") != self.point_no:
                continue
            if ev.get("type") not in ("throw", "huck"):
                continue
            if ev.get("outcome") != "completed":
                continue
            if ev.get("thrower") != x:
                continue
            if y is not None and ev.get("receiver") != y:
                continue
            ts_epoch = ev.get("ts_epoch", now)
            if now - ts_epoch <= within:
                return ev
            else:
                break  # older than window; stop scanning
        return None

    # If an assist was just logged for X, suppress immediate follow-up throw/huck
    def _has_recent_assist(self, x, within=DEDUPE_WINDOW_SEC):
        now = self._now()
        for ev in reversed(self.events):
            if ev.get("game") != self.game_no or ev.get("point") != self.point_no:
                continue
            if ev.get("type") != "assist":
                continue
            if ev.get("player") != x:
                continue
            ts_epoch = ev.get("ts_epoch", now)
            return (now - ts_epoch) <= within
        return False

    # --- control ---
    def new_game(self):
        self._push_history()
        self.game_no += 1
        self.point_no = 1
        self.events.clear()
        for p in self.players.values():
            p.update({"ds": 0, "assists": 0, "scores": 0,
                      "throws": 0, "completions": 0, "turnovers": 0,
                      "hucks": 0, "huck_completions": 0,
                      "drops": 0})
        now = self._now()
        self._last_point_change = now
        self.add_event({"ts_epoch": now, "t": ts(), "type": "new_game",
                        "game": self.game_no, "point": self.point_no})

    def new_point(self, *, push_history=True, reason="manual"):
        """
        Starts a new point:
          - If reason == "auto", we throttle so it won't trigger twice within 1s.
          - push_history=False lets us group with the preceding stat for single-step Undo.
        """
        now = self._now()
        if reason == "auto" and (now - self._last_point_change) < 1.0:
            return
        if push_history:
            self._push_history()
        self.point_no += 1
        self._last_point_change = now
        self.add_event({"ts_epoch": now, "t": ts(), "type": "new_point",
                        "game": self.game_no, "point": self.point_no})

    # --- events ---
    def record_d(self, x):
        self._push_history()
        x = self._ensure(x)
        if not x: return
        self.players[x]["ds"] += 1
        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "d",
                        "player": x, "game": self.game_no, "point": self.point_no})

    def record_drop(self, x):
        """Increment only the 'drops' counter for player X and log an event."""
        self._push_history()
        x = self._ensure(x)
        if not x: return
        self.players[x]["drops"] += 1
        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "drop",
                        "player": x, "game": self.game_no, "point": self.point_no})

    def record_assist(self, x):
        """
        Assist should count as:
          - Normally: assist + throw + completion
          - If it immediately follows a completed throw/huck by X (within window):
              ONLY assist (+ score if we can infer receiver), NO extra throw/completion.
        Also: automatically start a NEW POINT (auto, no extra history snapshot).
        """
        self._push_history()
        x = self._ensure(x)
        if not x: return

        recent = self._find_recent_completed(x, y=None, within=DEDUPE_WINDOW_SEC)
        if recent:
            # Only assist (and inferred score if possible)
            self.players[x]["assists"] += 1
            self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "assist",
                            "player": x, "game": self.game_no, "point": self.point_no})
            y = recent.get("receiver")
            if y:
                self.players[y]["scores"] += 1
                self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "score",
                                "player": y, "game": self.game_no, "point": self.point_no})
            # AUTO: new point
            self.new_point(push_history=False, reason="auto")
            return

        # Normal path
        self.players[x]["assists"] += 1
        self.players[x]["throws"] += 1
        self.players[x]["completions"] += 1
        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "assist",
                        "player": x, "game": self.game_no, "point": self.point_no})
        # AUTO: new point
        self.new_point(push_history=False, reason="auto")

    def record_score(self, y):
        self._push_history()
        y = self._ensure(y)
        if not y: return
        self.players[y]["scores"] += 1
        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "score",
                        "player": y, "game": self.game_no, "point": self.point_no})

    def record_throw(self, x, y, result):
        """
        Throw:
          - always increments throws
          - if completed -> completion
          - if turn -> turnover (no completion)
        If an assist was just logged for X within the window, we suppress this throw to avoid double counting.
        """
        self._push_history()
        x = self._ensure(x); y = self._ensure(y)
        if not x or not y: return

        if self._has_recent_assist(x, within=DEDUPE_WINDOW_SEC):
            return

        self.players[x]["throws"] += 1
        if result == "turn":
            self.players[x]["turnovers"] += 1
        else:
            self.players[x]["completions"] += 1

        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "throw",
                        "thrower": x, "receiver": y, "outcome": result,
                        "game": self.game_no, "point": self.point_no})

    def record_huck(self, x, y, result):
        """
        Huck:
          - counts as a throw and a huck
          - if completed -> completion + huck_completion
          - if turn -> turnover (no completion)
        If an assist was just logged for X within the window, we suppress this huck to avoid double counting.
        """
        self._push_history()
        x = self._ensure(x); y = self._ensure(y)
        if not x or not y: return

        if self._has_recent_assist(x, within=DEDUPE_WINDOW_SEC):
            return

        self.players[x]["throws"] += 1
        self.players[x]["hucks"] += 1
        if result == "turn":
            self.players[x]["turnovers"] += 1
        else:
            self.players[x]["completions"] += 1
            self.players[x]["huck_completions"] += 1

        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "huck",
                        "thrower": x, "receiver": y, "outcome": result,
                        "game": self.game_no, "point": self.point_no})

    def record_assist_to(self, x, y):
        """
        Assist(X) + Score(Y), with dedupe:
          - If a completed throw/huck X→Y just happened within the window:
              ONLY assist for X + score for Y. (NO extra throw/completion.)
          - Otherwise:
              assist + throw + completion for X, and score for Y.
        AUTO: always start a NEW POINT right after.
        """
        self._push_history()
        x = self._ensure(x); y = self._ensure(y)
        if not x or not y: return

        recent = self._find_recent_completed(x, y=y, within=DEDUPE_WINDOW_SEC)
        if recent:
            self.players[x]["assists"] += 1
            self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "assist",
                            "player": x, "game": self.game_no, "point": self.point_no})
            self.players[y]["scores"] += 1
            self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "score",
                            "player": y, "game": self.game_no, "point": self.point_no})
            # AUTO: new point
            self.new_point(push_history=False, reason="auto")
            return

        # Normal path
        self.players[x]["assists"] += 1
        self.players[x]["throws"] += 1
        self.players[x]["completions"] += 1
        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "assist",
                        "player": x, "game": self.game_no, "point": self.point_no})
        self.players[y]["scores"] += 1
        self.add_event({"ts_epoch": self._now(), "t": ts(), "type": "score",
                        "player": y, "game": self.game_no, "point": self.point_no})
        # AUTO: new point
        self.new_point(push_history=False, reason="auto")

BOOK = StatBook(ROSTER)

# ---- Grammar & parsing ----
RESULT_WORDS = {
    "complete": "completed", "completion": "completed", "completed": "completed",
    "turnover": "turn", "turn": "turn", "turned": "turn",
}
def norm_result(w):
    return RESULT_WORDS.get((w or "").lower(), (w or "").lower()) if w else None

def build_name_regex(roster):
    pats = []
    for n in sorted([r.strip().lower() for r in roster if r.strip()], key=len, reverse=True):
        pats.append(re.sub(r"\s+", r"\\s+", re.escape(n)))
    return r"(?:%s)" % "|".join(pats) if pats else r"(?:)"

def build_grammar_phrases(roster):
    roster = [r.strip().lower() for r in roster if r.strip()]
    phrases = set()
    phrases.update([
        "new game", "new point",
        "undo", "undo last", "revert", "go back", "scratch that", "cancel last",
    ])
    outcomes = ["completed", "complete", "completion", "turn", "turnover", "turned"]
    verbs_throw = ["threw", "throw", "passed"]
    verbs_huck  = ["huck", "hucked", "hook"]

    # single-name events
    for n in roster:
        phrases.add(f"{n} d")
        phrases.add(f"{n} assist"); phrases.add(f"{n} assists")
        phrases.add(f"{n} score");  phrases.add(f"{n} scores")
        phrases.add(f"{n} drop");   phrases.add(f"{n} drops")

    # pair events (throws, hucks, bare "x to y", "x turn to y", and "x assist(s) to y")
    for x in roster:
        for y in roster:
            if x == y: continue
            # Bare "x to y" (+ optional trailing result)
            phrases.add(f"{x} to {y}")
            for r in outcomes: phrases.add(f"{x} to {y} {r}")

            # "x turn/turnover to y" (result before 'to')
            phrases.add(f"{x} turn to {y}")
            phrases.add(f"{x} turnover to {y}")

            # Throw verbs
            for v in verbs_throw:
                phrases.add(f"{x} {v} to {y}")
                for r in outcomes: phrases.add(f"{x} {v} to {y} {r}")

            # Huck verbs
            for v in verbs_huck:
                phrases.add(f"{x} {v} to {y}")
                for r in outcomes: phrases.add(f"{x} {v} to {y} {r}")

            # Assist to
            phrases.add(f"{x} assist to {y}")
            phrases.add(f"{x} assists to {y}")
    return sorted(phrases)

def parse_command(text, roster_regex):
    s = (text or "").strip().lower()
    s = re.sub(r"\b(uh|um|like|and|then|now|okay|ok|alright)\b", "", s).strip()

    # accept if control word (new/undo) or any roster name appears
    if not (
        re.search(r"\bnew\s+(?:game|point)\b", s)
        or re.search(r"\b(undo|revert|go back|scratch that|cancel last)\b", s)
        or re.search(rf"\b{roster_regex}\b", s)
    ):
        return (None, {})

    if re.search(r"\bundo\b|\brevert\b|\bgo back\b|\bscratch that\b|\bcancel last\b", s):
        return ("undo", {})
    if re.search(r"\bnew\s+game\b", s):  return ("new_game", {})
    if re.search(r"\bnew\s+point\b", s): return ("new_point", {})

    name_pat = roster_regex

    # single-name
    m = re.match(rf"^\s*(?P<x>{name_pat})\s+d\b", s)
    if m: return ("d", {"x": re.sub(r"\s+", " ", m.group("x")).strip()})

    m = re.match(rf"^\s*(?P<x>{name_pat})\s+drops?\b$", s)
    if m: return ("drop", {"x": re.sub(r"\s+", " ", m.group("x")).strip()})

    m = re.match(rf"^\s*(?P<x>{name_pat})\s+assists?\b$", s)
    if m: return ("assist", {"x": re.sub(r"\s+", " ", m.group("x")).strip()})

    m = re.match(rf"^\s*(?P<y>{name_pat})\s+scores?\b$", s)
    if m: return ("score", {"y": re.sub(r"\s+", " ", m.group("y")).strip()})

    # assist to
    m = re.match(rf"^\s*(?P<x>{name_pat})\s+assists?\s+to\s+(?P<y>{name_pat})\s*$", s)
    if m:
        return ("assist_to", {
            "x": re.sub(r"\s+", " ", m.group("x")).strip(),
            "y": re.sub(r"\s+", " ", m.group("y")).strip()
        })

    # "x (turn|turnover|completed|completion) to y"  (result before "to")
    m = re.match(
        rf"^\s*(?P<x>{name_pat})\s+(?P<r>completed|complete|completion|turn|turnover|turned)\s+(?:to|2)\s+(?P<y>{name_pat})\s*$",
        s
    )
    if m:
        return ("throw", {
            "x": re.sub(r"\s+", " ", m.group("x")).strip(),
            "y": re.sub(r"\s+", " ", m.group("y")).strip(),
            "result": norm_result(m.group("r"))
        })

    # bare "x to y [result]" => throw
    m = re.match(
        rf"^\s*(?P<x>{name_pat})\s+(?:to|2)\s+(?P<y>{name_pat})(?:\s+(?P<r>completed|complete|completion|turn|turnover|turned))?\s*$",
        s
    )
    if m:
        return ("throw", {
            "x": re.sub(r"\s+", " ", m.group("x")).strip(),
            "y": re.sub(r"\s+", " ", m.group("y")).strip(),
            "result": norm_result(m.group("r"))
        })

    # throw with verb
    m = re.match(
        rf"^\s*(?P<x>{name_pat})\s+(?:threw|throw|passed?)\s+(?:to|2)\s+(?P<y>{name_pat})(?:\s+(?P<r>completed|complete|completion|turn|turnover|turned))?\s*$",
        s
    )
    if m:
        return ("throw", {
            "x": re.sub(r"\s+", " ", m.group("x")).strip(),
            "y": re.sub(r"\s+", " ", m.group("y")).strip(),
            "result": norm_result(m.group("r"))
        })

    # huck
    m = re.match(
        rf"^\s*(?P<x>{name_pat})\s+(?:huck|hucked|hook)\s+(?:to\s+)?(?P<y>{name_pat})(?:\s+(?P<r>completed|complete|completion|turn|turnover|turned))?\s*$",
        s
    )
    if m:
        return ("huck", {
            "x": re.sub(r"\s+", " ", m.group("x")).strip(),
            "y": re.sub(r"\s+", " ", m.group("y")).strip(),
            "result": norm_result(m.group("r"))
        })

    return (None, {})

# ---- Recognizer helpers ----
def ensure_model():
    global _model
    if _model is None:
        if not os.path.exists(MODEL_PATH):
            raise RuntimeError(f"Vosk model not found at {MODEL_PATH}")
        logger.info("Loading Vosk model…")
        _model = Model(MODEL_PATH)
    return _model

def make_recognizer(roster):
    phrases = build_grammar_phrases(roster)
    grammar_json = json.dumps(phrases)
    rec = KaldiRecognizer(ensure_model(), SAMPLE_RATE, grammar_json)
    rec.SetWords(False)
    return rec

def ensure_recognizer():
    global REC
    with LOCK:
        if REC is None:
            REC = make_recognizer(BOOK.roster)
    return REC

# ---- Broadcasting ----
def broadcast_stats():
    rows = [{"player": name, **stats} for name, stats in BOOK.players.items()]
    rows.sort(key=lambda r: (r["scores"], r["assists"], r["ds"], r["completions"], r["throws"]), reverse=True)

    recent = BOOK.events[-200:]
    recent = list(reversed(recent))  # newest first

    socketio.emit("stats", {
        "game": BOOK.game_no,
        "point": BOOK.point_no,
        "players": rows,
        "pairs": [],
        "events": recent,
    }, namespace="/")

# ---- CSV persistence (per game folder, overall + per-point) ----
def _ensure_game_dir(game_no: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    dir_name = f"game {game_no}"  # e.g., "game 2"
    full = os.path.join(DATA_DIR, dir_name)
    os.makedirs(full, exist_ok=True)
    return dir_name, full

def export_csvs():
    """
    Writes into:
      /data/game {G}/game_{G}_overall.csv
      /data/game {G}/point_1.csv, /point_2.csv, ... up to current point_no
    """
    try:
        game_no = BOOK.game_no
        dir_rel, dir_abs = _ensure_game_dir(game_no)

        written_rel = []

        # Overall stats (one row per player)
        overall_path_abs = os.path.join(dir_abs, f"game_{game_no}_overall.csv")
        overall_path_rel = f"{dir_rel}/game_{game_no}_overall.csv"
        pfields = ["player","throws","completions","turnovers","drops",
                   "hucks","huck_completions","ds","assists","scores"]
        with open(overall_path_abs, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=pfields)
            w.writeheader()
            for name, st in sorted(BOOK.players.items()):
                w.writerow({
                    "player": name,
                    "throws": st["throws"],
                    "completions": st["completions"],
                    "turnovers": st["turnovers"],
                    "drops": st["drops"],
                    "hucks": st["hucks"],
                    "huck_completions": st["huck_completions"],
                    "ds": st["ds"],
                    "assists": st["assists"],
                    "scores": st["scores"],
                })
        written_rel.append(overall_path_rel)

        # Per-point events (skip new_game/new_point)
        include_types = {"throw","huck","assist","score","d","drop"}
        efields = ["t","type","game","point","player","thrower","receiver","outcome"]

        max_point = BOOK.point_no
        for p in range(1, max_point + 1):
            rows = [ev for ev in BOOK.events if ev.get("point") == p and ev.get("type") in include_types]
            pt_path_abs = os.path.join(dir_abs, f"point_{p}.csv")
            pt_path_rel = f"{dir_rel}/point_{p}.csv"
            with open(pt_path_abs, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=efields)
                w.writeheader()
                for ev in rows:
                    w.writerow({k: ev.get(k, "") for k in efields})
            written_rel.append(pt_path_rel)

        logger.info(f"CSV saved to {dir_abs}: {len(written_rel)} file(s)")
        return {"game_dir_rel": dir_rel, "written_rel": written_rel}
    except Exception:
        logger.exception("CSV export failed")
        raise

def autosave_loop():
    logger.info("autosave: start (120s)")
    while True:
        eventlet.sleep(120.0)
        try:
            export_csvs()
        except Exception:
            pass  # error already logged

# ---- Socket.IO handlers ----
@socketio.on("connect")
def on_connect(auth=None):
    logger.info("Client connected")
    ensure_recognizer()
    socketio.emit("status", {"msg": "connected"}, to=request.sid, namespace="/")
    broadcast_stats()

@socketio.on("disconnect")
def on_disconnect():
    METRICS["disconnects"] += 1
    logger.warning("Client disconnected")

@socketio.on("command")
def on_command(data):
    cmd = (data or {}).get("cmd")
    if cmd == "new_game":
        BOOK.new_game()
    elif cmd == "new_point":
        BOOK.new_point()  # manual => history snapshot
    elif cmd == "undo":
        if BOOK.undo():
            socketio.emit("status", {"msg": "Undid last action"}, to=request.sid, namespace="/")
        else:
            socketio.emit("status", {"msg": "Nothing to undo"}, to=request.sid, namespace="/")
    elif cmd == "export":
        try:
            manifest = export_csvs()
            socketio.emit("status", {"msg": f"Saved CSVs to /data/{manifest['game_dir_rel']}"}, to=request.sid, namespace="/")
        except Exception:
            socketio.emit("status", {"msg": "Save failed (see server logs)"}, to=request.sid, namespace="/")
    broadcast_stats()

@socketio.on("audio_chunk")
def on_audio_chunk(data):
    if not data: return
    try:
        AUDIO_Q.put_nowait(data)
        METRICS["chunks_enqueued"] += 1
        qsz = AUDIO_Q.qsize()
        if qsz > METRICS["q_max"]:
            METRICS["q_max"] = qsz
        METRICS["last_chunk_ts"] = time.time()
    except Exception:
        # queue full — drop one to stay realtime
        try:
            _ = AUDIO_Q.get_nowait()
            METRICS["chunks_dropped"] += 1
        except Exception:
            pass
        AUDIO_Q.put_nowait(data)

# ---- Worker ----
def audio_worker_loop():
    logger.info("worker: start")
    rec = ensure_recognizer()
    name_regex = build_name_regex(BOOK.roster)

    global LAST_PARTIAL
    last_emit = 0.0

    while True:
        try:
            chunk = AUDIO_Q.get(timeout=0.5)
        except Empty:
            eventlet.sleep(0); continue

        try:
            with LOCK:
                ok = rec.AcceptWaveform(chunk)
        except Exception:
            logger.exception("AcceptWaveform crashed")
            eventlet.sleep(0); continue

        METRICS["chunks_processed"] += 1

        if not ok:
            # Emit partial transcript to UI
            try:
                partial = (json.loads(rec.PartialResult()).get("partial") or "").strip()
            except Exception:
                partial = ""
            if partial and partial != LAST_PARTIAL:
                LAST_PARTIAL = partial
                socketio.emit("partial", {"text": partial}, namespace="/")
            eventlet.sleep(0); continue

        # Final result
        try:
            res = json.loads(rec.Result())
        except Exception:
            logger.exception("Result JSON failed")
            eventlet.sleep(0); continue

        text = (res.get("text") or "").strip()
        METRICS["last_result_ts"] = time.time()

        # Always tell client the final transcript
        socketio.emit("final", {"text": text}, namespace="/")
        LAST_PARTIAL = ""

        if not text:
            eventlet.sleep(0); continue

        # Parse → action/payload
        action, payload = parse_command(text, name_regex)
        if not action:
            eventlet.sleep(0); continue

        # Default outcomes for throws/hucks
        if action in ("throw", "huck"):
            r = payload.get("result")
            if r is None or r in ("", "complete", "completion", "completed"):
                payload["result"] = "completed"

        # Required fields guard
        REQUIRED = {
            "new_game": [], "new_point": [], "undo": [],
            "d": ["x"], "drop": ["x"], "assist": ["x"], "score": ["y"],
            "assist_to": ["x","y"],
            "throw": ["x","y","result"], "huck": ["x","y","result"],
        }
        missing = [k for k in REQUIRED[action] if not payload.get(k)]
        if missing:
            logger.debug(f"ignore incomplete {action}: missing {missing} in '{text}'")
            eventlet.sleep(0); continue

        # Apply
        try:
            if action == "undo":
                if BOOK.undo():
                    socketio.emit("status", {"msg": "Undid last action"}, namespace="/")
                else:
                    socketio.emit("status", {"msg": "Nothing to undo"}, namespace="/")
            elif action == "new_game":
                BOOK.new_game()
            elif action == "new_point":
                BOOK.new_point()
            elif action == "d":
                BOOK.record_d(payload["x"])
            elif action == "drop":
                BOOK.record_drop(payload["x"])
            elif action == "assist":
                BOOK.record_assist(payload["x"])
            elif action == "score":
                BOOK.record_score(payload["y"])
            elif action == "assist_to":
                BOOK.record_assist_to(payload["x"], payload["y"])
            elif action == "throw":
                BOOK.record_throw(payload["x"], payload["y"], payload["result"])
            elif action == "huck":
                BOOK.record_huck(payload["x"], payload["y"], payload["result"])

            METRICS["commands_applied"] += 1
            logger.info(f"APPLIED {action} {payload}  (total={METRICS['commands_applied']})")
        except Exception:
            logger.exception("apply_event failed")
            eventlet.sleep(0); continue

        # Throttle UI refresh a bit
        now = time.time()
        if now - last_emit > 0.05:
            broadcast_stats()
            last_emit = now

        eventlet.sleep(0)

# ---- Routes ----
@app.get("/")
def index():
    return render_template("index.html", game_no=BOOK.game_no, point_no=BOOK.point_no)

@app.get("/health")
def health():
    return {"ok": True, "game": BOOK.game_no, "point": BOOK.point_no}

@app.get("/debug")
def debug():
    return {
        "metrics": METRICS,
        "queue_size": AUDIO_Q.qsize(),
        "roster": BOOK.roster,
        "events": len(BOOK.events),
    }

@app.post("/export")
def export_now():
    try:
        manifest = export_csvs()
        return jsonify({"ok": True, "dir": DATA_DIR, **manifest})
    except Exception as e:
        logger.exception("manual export failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/download/<path:fname>")
def download_file(fname):
    return send_from_directory(DATA_DIR, fname, as_attachment=True)

# ---- Main ----
if __name__ == "__main__":
    socketio.start_background_task(audio_worker_loop)
    if AUTOSAVE == "1":
        socketio.start_background_task(autosave_loop)
    socketio.run(app, host="0.0.0.0", port=8000, debug=False)
