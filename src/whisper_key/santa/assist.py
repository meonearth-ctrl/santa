# santa/assist.py
# Live Assist: Claude helps the hiring panel during a live interview session
# (santa/live.py). Two jobs, ported from Hiring Right's tested Node prototypes:
#   probes   after each candidate answer, suggest the next 1-2 questions from
#            the approved probe bank (replay.mjs, "hybrid" rules)
#   rating   after each activity the panel marks done, and at Stop, re-score
#            every seat as a silent third rater (rate.mjs)
# The panel sees cards, coverage and (lead only, behind a toggle) ratings on
# their phones through Santa's phone mode. People make every decision.
#
# Only transcript text goes to the Claude API; audio and video never leave the
# Mac. The API key is read here and never logged or sent to a phone. Each job
# is one append-only conversation (cached system prompt + growing transcript),
# run on its own worker thread so calls stay in order. Spend is capped per
# session and per day. At Stop, <label>.assist.json is written for Hiring Right.

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import date, datetime, timezone

from . import assist_prompts as ap
from .export import _atomic_write, export_stem, resolve_dir

logger = logging.getLogger(__name__)

# ── Rules from the prototypes and LIVE_ASSIST_SPEC.md ────────────────────────
MIN_TURN = 12             # seconds of candidate speech before an answer counts as finished
COOLDOWN = 45             # at most one suggestion call per 45 s per room
PAUSE = 2                 # the call goes out 2 s after the answer ends
END_GAP = 4               # an untagged line more than 4 s after the answer ends it
TRANSCRIPT_LAG = 3        # live only: allow this long for a following line to be transcribed
STALE = 360               # a card older than 6 minutes is dropped
MIN_ACTIVITY = 120        # rate after activities that ran 2 minutes or more
HOLD_TYPES = {'briefing', 'intro', 'group_activity', 'roleplay'}   # "Ask after this activity"
PROBE_EFFORT, RATING_EFFORT = 'low', 'medium'
PROBE_MAX_TOKENS, RATING_MAX_TOKENS = 4000, 16000
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
KEY_PATH = '~/.config/hiring-right/anthropic-key'
ASSIST_SUFFIX = '.assist.json'


class AssistError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def api_key():
    """ANTHROPIC_API_KEY, else the one-line key file. Never logged."""
    if os.environ.get('ANTHROPIC_API_KEY'):
        return os.environ['ANTHROPIC_API_KEY'].strip()
    try:
        with open(os.path.expanduser(KEY_PATH), encoding='utf-8') as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def make_client(key: str):
    import anthropic
    return anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=2)


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds')


# ── When to ask for probes (replay.mjs eventsOf, run line by line) ───────────
class ProbeTrigger:
    """An event fires when a seat-tagged candidate has spoken for MIN_TURN s and
    then someone else speaks, the panel speaks, or an untagged line comes more
    than END_GAP s later; at most one per COOLDOWN. Live, a silence is noticed
    by tick() once no new line has arrived for END_GAP + TRANSCRIPT_LAG s."""

    def __init__(self):
        self.seat, self.speech, self.end, self.last = None, 0.0, 0.0, -1e9

    def line(self, seg: dict):
        """Feed one tagged line; returns an event {'at', 'seat'} or None. The
        event belongs BEFORE this line (the line itself goes to the next call)."""
        seat, event = seg.get('seat'), None
        if isinstance(seat, int):
            if self.seat != seat:
                event = self._fire()
                self.seat = seat
            self.speech += seg['end'] - seg['start']
            self.end = seg['end']
        elif seat == 'panel' or seg['start'] - self.end > END_GAP:
            event = self._fire()
        return event

    def tick(self, audio_now: float):
        if self.seat is not None and audio_now - self.end > END_GAP + TRANSCRIPT_LAG:
            return self._fire()
        return None

    def _fire(self):
        event = None
        if self.seat is not None and self.speech >= MIN_TURN and self.end + PAUSE - self.last >= COOLDOWN:
            event = {'at': round(self.end + PAUSE, 2), 'seat': self.seat}
            self.last = self.end + PAUSE
        self.seat, self.speech = None, 0.0
        return event


# ── Spend: per session and per day ───────────────────────────────────────────
class SpendLedger:
    """Daily totals in <santa home>/assist/spend.json, so the daily cap holds
    across sessions and restarts."""

    def __init__(self, home: str):
        self.path = os.path.join(home, 'assist', 'spend.json')
        self._lock = threading.Lock()

    def today(self) -> float:
        return float(self._read().get(date.today().isoformat(), 0.0))

    def add(self, dollars: float) -> None:
        with self._lock:
            data = self._read()
            key = date.today().isoformat()
            data[key] = round(float(data.get(key, 0.0)) + dollars, 6)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            _atomic_write(self.path, json.dumps(data, indent=1))

    def _read(self) -> dict:
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


# ── One append-only Claude conversation, served by its own thread ────────────
class Conversation:
    """System prompt cached; each call appends the new user turn, and the reply
    is appended back exactly as returned, so the cached prefix stays valid."""

    def __init__(self, client, model: str, effort: str, max_tokens: int, system: str, schema: dict):
        self.client, self.model, self.effort, self.max_tokens = client, model, effort, max_tokens
        self.system = [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}]
        self.schema = schema
        self.messages = []

    def ask(self, content: str) -> dict:
        """One call. Returns {'out', 'usage', 'ms', 'stop', 'cost'}; on an API
        error the user turn is taken back out and the error re-raised."""
        self.messages.append({'role': 'user', 'content': content})
        started = time.time()
        request = dict(model=self.model, max_tokens=self.max_tokens, system=self.system,
                       cache_control={'type': 'ephemeral'},   # also caches the growing transcript
                       messages=self.messages,
                       output_config={'effort': self.effort, 'format': {'type': 'json_schema', 'schema': self.schema}})
        try:
            if self.model == 'claude-haiku-5-5':           # no server-side fallback for Haiku
                response = self.client.messages.create(**request)
            else:
                # On a safety decline the API re-runs the call on the recommended fallback model.
                response = self.client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks='default')
        except Exception:
            self.messages.pop()
            raise
        self.messages.append({'role': 'assistant', 'content': [block.to_dict() for block in response.content]})
        text = ''.join(block.text for block in response.content if block.type == 'text')
        out = None
        if response.stop_reason != 'refusal':
            try:
                out = json.loads(text)
            except ValueError:
                out = None                                  # recorded as a parse failure
        usage = response.usage.to_dict() if hasattr(response.usage, 'to_dict') else dict(response.usage)
        return {'out': out, 'usage': usage, 'ms': int((time.time() - started) * 1000),
                'stop': response.stop_reason, 'model': response.model, 'cost': ap.cost_of(self.model, usage)}


# ── One Live Assist session ──────────────────────────────────────────────────
class AssistSession:
    """Attached to a LiveSession: receives its tagged lines and audio clock,
    runs the probe and rating conversations, keeps the panel's state."""

    def __init__(self, label: str, track: str, roster: list, cfg: dict, client, settings: dict,
                 ledger: SpendLedger, export_dir: str, source_name: str):
        if track not in ('team', 'manager'):
            raise AssistError('bad_track', 'Track must be "team" or "manager".')
        self.label, self.track, self.cfg, self.ledger = label, track, cfg, ledger
        self.roster = roster or []
        self.source_name = source_name
        self.path = os.path.join(resolve_dir(export_dir), export_stem(label + '.wav') + ASSIST_SUFFIX)
        self.models = {'probes': settings['assist_probe_model'], 'rating': settings['assist_rating_model']}
        self.session_cap, self.daily_cap = settings['assist_session_budget'], settings['assist_daily_budget']
        self.probes = Conversation(client, self.models['probes'], PROBE_EFFORT, PROBE_MAX_TOKENS,
                                   ap.probe_system(cfg, track), ap.PROBE_SCHEMA)
        self.rater = Conversation(client, self.models['rating'], RATING_EFFORT, RATING_MAX_TOKENS,
                                  ap.rating_system(cfg, track), ap.rating_schema(cfg, track))
        self.steps = ap.run_steps(cfg, track)
        self.trigger = ProbeTrigger()
        self.started, self.ended = time.time(), None
        self.audio_now = 0.0
        self.activities = []                  # {'id', 'name', 'type', 'start', 'end'}
        self.script = []                      # replays: recorded phases applied as the audio clock passes
        self.cards = []                       # suggestion cards, newest last
        self.gaps, self.quiet = [], []
        self.ratings = []                     # every rating call: {'at', 'trigger', 'out', ...}
        self.calls = []                       # usage log per call (no transcript text)
        self.cost = {'probes': 0.0, 'rating': 0.0}
        self.capped = None                    # message once a budget stops the calls
        self.error = None
        self.state = 'running'                # running | finishing | done
        self._lock = threading.Lock()
        self._unsent_probe = []               # lines not yet sent to the probe conversation
        self._unsent_rating = []
        self._probe_head = self._rating_head = True
        self._jobs = {'probes': queue.Queue(), 'rating': queue.Queue()}
        self._workers = [threading.Thread(target=self._work, args=(name,), daemon=True,
                                          name=f'santa-assist-{name}') for name in self._jobs]
        for worker in self._workers:
            worker.start()

    # ── Input from the live session ──────────────────────────────────────
    def add_lines(self, segments: list, audio_now: float) -> None:
        """New tagged lines from the live transcriber (in order)."""
        with self._lock:
            self.audio_now = max(self.audio_now, audio_now)
            for seg in segments:
                event = self.trigger.line(seg)
                if event:
                    self._queue_probe(event)           # before this line is added: it belongs to the next call
                self._unsent_probe.append(seg)
                self._unsent_rating.append(seg)

    def tick(self, audio_now: float) -> None:
        """Called about once a second with the session's audio clock."""
        with self._lock:
            self.audio_now = max(self.audio_now, audio_now)
            self._play_script()
            event = self.trigger.tick(self.audio_now)
            if event:
                self._queue_probe(event)

    # ── Input from the panel ─────────────────────────────────────────────
    def activity(self, step_id: str, action: str) -> dict:
        """Start or finish a run step. Starting one finishes the step that was running."""
        with self._lock:
            step = next((s for s in self.steps if s['id'] == step_id), None)
            if step is None or action not in ('start', 'finish'):
                raise AssistError('bad_step', 'Unknown activity step.')
            running = self._running()
            if running and (action == 'start' or running['id'] == step_id):
                self._finish_activity(running)
            if action == 'start':
                self.activities.append({'id': step['id'], 'name': step['name'], 'type': step['type'],
                                        'start': round(self.audio_now, 2), 'end': None})
            return self._steps_view()

    def play_phases(self, phases: list) -> None:
        """Replays: the activities a recording really had (Hiring Right's phases.json,
        {name, type, start, end}) stand in for the panel's taps on the activity strip."""
        with self._lock:
            self.script = sorted(({'name': p['name'], 'type': p['type'], 'start': float(p['start']),
                                   'end': float(p['end']), 'started': False} for p in phases),
                                 key=lambda p: p['start'])

    def _play_script(self) -> None:
        # Caller holds the lock.
        for phase in self.script:
            if not phase['started'] and self.audio_now >= phase['start']:
                running = self._running()
                if running:
                    self._finish_activity(running)
                self.activities.append({'id': None, 'name': phase['name'], 'type': phase['type'],
                                        'start': phase['start'], 'end': None})
                phase['started'] = True
            running = self._running()
            if phase['started'] and running and running['name'] == phase['name'] and self.audio_now >= phase['end']:
                self._finish_activity(running, at=phase['end'])

    def mark_card(self, card_id: str, status: str) -> bool:
        if status not in ('asked', 'skipped'):
            raise AssistError('bad_status', 'Status must be asked or skipped.')
        with self._lock:
            for card in self.cards:
                if card['id'] == card_id:
                    card['status'], card['marked_at'] = status, round(self.audio_now, 2)
                    return True
        return False

    # ── Stop: final rating, then the output file ─────────────────────────
    def finish(self, audio_seconds: float) -> None:
        """At Stop: finish the running activity, queue the final rating, wait for
        both conversations to drain and write <label>.assist.json."""
        with self._lock:
            self.audio_now = max(self.audio_now, audio_seconds)
            running = self._running()
            if running:
                self._finish_activity(running, rate=False)
            self.state = 'finishing'
            self._queue_rating({'at': round(self.audio_now, 2), 'trigger': 'final',
                                'why': 'The interview has ended. This is your final live rating.'})
            for jobs in self._jobs.values():
                jobs.put(None)
        for worker in self._workers:
            worker.join(timeout=300)
        self.ended = time.time()
        with self._lock:
            self.state = 'done'
            self._expire_cards()
        self.write()

    # ── Queueing calls ───────────────────────────────────────────────────
    def _queue_probe(self, event: dict) -> None:
        # Caller holds the lock. Lines so far go with this call.
        lines, self._unsent_probe = self._unsent_probe, []
        self._jobs['probes'].put({'event': event, 'lines': lines, 'label': self._card_label()})

    def _queue_rating(self, point: dict) -> None:
        lines, self._unsent_rating = self._unsent_rating, []
        if not lines and point['trigger'] != 'final':
            return                                        # nothing new since the last rating
        self._jobs['rating'].put(dict(point, lines=lines))

    def _finish_activity(self, act: dict, rate: bool = True, at: float = None) -> None:
        act['end'] = round(self.audio_now if at is None else at, 2)
        if rate and act['end'] - act['start'] >= MIN_ACTIVITY:
            self._queue_rating({'at': act['end'], 'trigger': 'activity_end', 'activity': act['name'],
                                'why': f'The activity "{act["name"]}" ({act["type"]}) has just ended.'})

    def _running(self):
        return next((a for a in reversed(self.activities) if a['end'] is None), None)

    def _card_label(self) -> str:
        running = self._running()
        return 'after_activity' if running and running['type'] in HOLD_TYPES else 'now'

    # ── Workers ──────────────────────────────────────────────────────────
    def _work(self, name: str) -> None:
        jobs = self._jobs[name]
        while True:
            job = jobs.get()
            if job is None:
                return
            if not self._budget_ok():
                continue
            try:
                if name == 'probes':
                    self._ask_probes(job)
                else:
                    self._ask_rating(job)
            except Exception as exc:
                # The lines go back to the front of the queue, so the next call still sends them.
                with self._lock:
                    if name == 'probes':
                        self._unsent_probe = job['lines'] + self._unsent_probe
                    else:
                        self._unsent_rating = job['lines'] + self._unsent_rating
                    self.error = f'Claude call failed ({type(exc).__name__}); will retry with the next one.'
                logger.warning('assist %s: %s call failed: %s', self.label, name, type(exc).__name__)

    def _ask_probes(self, job: dict) -> None:
        event = job['event']
        head = ap.group_head(self.label, self.track, self.roster) if self._probe_head else ''
        lines = '\n'.join(ap.line_of(seg) for seg in job['lines'])
        cue = f"#{event['seat']} has just finished an answer. What should the panel ask next?"
        result = self.probes.ask(f"{head}{lines}\n\n[{ap.mmss(event['at'])}] {cue}")
        self._probe_head = False
        self._account('probes', result, event['at'])
        out = result['out'] or {}
        with self._lock:
            shown = round(self.audio_now, 2)
            fresh = []
            for probe in out.get('probes') or []:
                for card in self.cards:                      # a newer card for the same seat replaces older ones
                    if card['seat'] == probe['seat'] and card['status'] == 'unseen':
                        card['status'] = 'dropped'
                fresh.append({'id': uuid.uuid4().hex[:10], 'at': event['at'], 'shownAt': shown,
                              'seat': probe['seat'], 'probe_id': probe['probe_id'], 'say': probe['say'],
                              'competency': probe['competency'], 'why': probe['why'],
                              'label': job['label'], 'status': 'unseen'})
            self.cards.extend(fresh)
            if result['out'] is not None:
                self.gaps, self.quiet = out.get('gaps') or [], out.get('quiet') or []
            self.error = None

    def _ask_rating(self, job: dict) -> None:
        head = ap.group_head(self.label, self.track, self.roster) if self._rating_head else ''
        lines = '\n'.join(ap.line_of(seg) for seg in job['lines']) or '(no new lines)'
        result = self.rater.ask(f"{head}{lines}\n\n[{ap.mmss(job['at'])}] {job['why']} Re-score every seat.")
        self._rating_head = False
        self._account('rating', result, job['at'])
        with self._lock:
            self.ratings.append({'at': job['at'], 'trigger': job['trigger'], 'out': result['out']})
            self.error = None

    def _account(self, kind: str, result: dict, at: float) -> None:
        """Cost and usage per call, to Santa's log and the output file (no transcript text)."""
        with self._lock:
            self.cost[kind] += result['cost']
            self.calls.append({'kind': kind, 'at': at, 'ms': result['ms'], 'stop': result['stop'],
                               'model': result['model'], 'usage': result['usage'],
                               'cost': round(result['cost'], 5), 'parsed': result['out'] is not None})
        self.ledger.add(result['cost'])
        u = result['usage']
        logger.info('assist %s: %s call %d ms, in %s, cache read %s, write %s, out %s tokens, $%.4f (%s)',
                    self.label, kind, result['ms'], u.get('input_tokens'), u.get('cache_read_input_tokens'),
                    u.get('cache_creation_input_tokens'), u.get('output_tokens'), result['cost'], result['stop'])

    def _budget_ok(self) -> bool:
        spent = self.cost['probes'] + self.cost['rating']
        if spent >= self.session_cap:
            self.capped = f'Session budget of ${self.session_cap:.2f} reached; Live Assist has stopped calling Claude.'
        elif self.ledger.today() >= self.daily_cap:
            self.capped = f'Daily budget of ${self.daily_cap:.2f} reached; Live Assist has stopped calling Claude.'
        else:
            return True
        logger.warning('assist %s: %s', self.label, self.capped)
        return False

    # ── What the panel sees ──────────────────────────────────────────────
    def _expire_cards(self) -> None:
        for card in self.cards:
            if card['status'] == 'unseen' and self.audio_now - card['at'] > STALE:
                card['status'] = 'dropped'

    def _steps_view(self) -> list:
        done = {a['id'] for a in self.activities if a['end'] is not None}
        running = self._running()
        return [dict(s, status='running' if running and running['id'] == s['id'] else
                     'done' if s['id'] in done else 'pending') for s in self.steps]

    def view(self, show_rating: bool) -> dict:
        """State for the phone page or the Mac window. Ratings only when allowed."""
        with self._lock:
            self._expire_cards()
            spent = self.cost['probes'] + self.cost['rating']
            view = {'label': self.label, 'track': self.track, 'state': self.state,
                    'audio_seconds': round(self.audio_now, 1), 'roster': self.roster,
                    'steps': self._steps_view(),
                    'cards': [c for c in self.cards if c['status'] == 'unseen'],
                    'gaps': self.gaps, 'quiet': self.quiet,
                    'spend': {'session': round(spent, 3), 'session_cap': self.session_cap,
                              'today': round(self.ledger.today(), 3), 'daily_cap': self.daily_cap},
                    'capped': self.capped, 'error': self.error,
                    'pending': {k: q.qsize() for k, q in self._jobs.items()}}
            if show_rating:
                view['rating'] = self._latest_rating_rows()
        return view

    def _latest_rating_rows(self) -> list:
        latest = next((r for r in reversed(self.ratings) if r['out']), None)
        if latest is None:
            return []
        rows = []
        for seat in latest['out'].get('seats') or []:
            scores = {r['key']: r['s'] for r in seat.get('ratings') or []}
            rows.append({'seat': seat['seat'], 'overall': ap.weighted_overall(self.cfg, self.track, scores),
                         'rec': seat.get('rec'), 'evidence': seat.get('evidence'), 'oneLine': seat.get('oneLine'),
                         'at': latest['at']})
        return sorted(rows, key=lambda r: r['seat'])

    # ── Output for Hiring Right ──────────────────────────────────────────
    def payload(self) -> dict:
        with self._lock:
            final_call = next((r for r in reversed(self.ratings) if r['out']), None)
            history = []
            for r in self.ratings:
                if not r['out']:
                    continue
                seats = []
                for seat in r['out'].get('seats') or []:
                    scores = {x['key']: x['s'] for x in seat.get('ratings') or []}
                    seats.append({'candNo': seat['seat'], 'overall': ap.weighted_overall(self.cfg, self.track, scores),
                                  'rec': seat.get('rec'), 'evidence': seat.get('evidence')})
                history.append({'at': r['at'], 'trigger': r['trigger'], 'seats': seats})
            return {
                'kind': 'hiring-right-live-assist', 'version': 1,
                'label': self.label, 'track': self.track, 'source': self.source_name,
                'models': dict(self.models),
                'started': _iso(self.started), 'ended': _iso(self.ended or time.time()),
                'cost': {'probes': round(self.cost['probes'], 4), 'rating': round(self.cost['rating'], 4),
                         'total': round(self.cost['probes'] + self.cost['rating'], 4)},
                'roster': [{'no': c['no'], 'name': c['name']} for c in self.roster],
                'activities': [{'name': a['name'], 'type': a['type'], 'start': a['start'], 'end': a['end']}
                               for a in self.activities],
                'suggestions': [{k: c[k] for k in ('at', 'shownAt', 'seat', 'probe_id', 'say', 'competency', 'why',
                                                   'label', 'status')} for c in self.cards],
                'final': [_final_row(seat) for seat in (final_call['out'].get('seats') or [])] if final_call else [],
                'history': history,
                'calls': list(self.calls),
                'capped': self.capped,
            }

    def write(self) -> str:
        _atomic_write(self.path, json.dumps(self.payload(), ensure_ascii=False, indent=1))
        logger.info('assist %s: wrote %s ($%.3f)', self.label, os.path.basename(self.path),
                    self.cost['probes'] + self.cost['rating'])
        return self.path


def _final_row(seat: dict) -> dict:
    """A rating seat in Hiring Right's score shape (ratings keyed by competency)."""
    return {'candNo': seat['seat'],
            'ratings': {r['key']: {'s': r['s'], 'ev': r['ev']} for r in seat.get('ratings') or []},
            'rec': seat.get('rec'), 'levelRec': seat.get('level') or '', 'brandRec': seat.get('brand') or '',
            'brandAlt': [], 'integrity': seat.get('integrity'), 'flags': seat.get('flags') or [],
            'oneLine': seat.get('oneLine'), 'standout': seat.get('standout'), 'concern': seat.get('concern'),
            'probe': seat.get('probe'), 'evidence': seat.get('evidence')}
