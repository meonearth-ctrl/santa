# santa/seat_tagger.py
# Python port of Hiring Right's seat tagger (hiring-right-seat-tagger.js, also
# 09_Live_Replay/tagger.cjs). It decides which numbered candidate seat is talking
# from what is SAID, not from the voice: a candidate saying their number
# ("Number four."), saying their name (matched to the group's roster), or the
# panel calling a number ("Candidate number two, …") just before someone answers.
#
# Keep this equivalent to the JavaScript so Santa's live tags and Hiring Right's
# tags agree: same regexes (ASCII word boundaries, like JS), same thresholds,
# same tie-breaking. tests/test_live.py replays every real group through both
# and compares the results line by line when Node is available.

import re

# ── Cue patterns (copied from the JS) ────────────────────────────────────────
NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
                'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12}
_NUM = r'(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'
# re.ASCII: JavaScript's \b and \d only know ASCII; Python's would also match
# Unicode letters/digits and tag differently on mixed-script lines.
_FLAGS = re.ASCII
_SELF_CUES = [
    re.compile(r"\b(?:i am|i'm|im|this is|my number is)\s+(?:candidate\s+)?number\s*" + _NUM + r"\b", _FLAGS),
    re.compile(r"\b(?:i am|i'm|im|this is)\s+candidate\s+" + _NUM + r"\b", _FLAGS),
    re.compile(r"\b(?:candidate|number)\s+(?:number\s+)?" + _NUM + r"\s*(?:here|speaking)\b", _FLAGS),
]
# A bare "Number four." (optionally after a greeting) said by the candidate.
_LEAD = re.compile(r"^(?:(?:hi|hello|good (?:morning|afternoon|evening|day))[\s,.!]*"
                   r"(?:everyone|all|sir|ma'am|maam|po|panel)?[\s,.!]*)?(?:candidate\s+)?number\s*"
                   + _NUM + r"[\s,.!]*$", _FLAGS)
_CALL = re.compile(r"\b(?:candidate\s+(?:number\s+)?|number\s+)" + _NUM + r"\b", _FLAGS)
_NAME_INTRO = re.compile(r"\b(?:my name is|my name's|you can call me|call me|i am|i'm)\s+([a-z][a-z' .-]{2,50})",
                         _FLAGS)
_NON_NAME = re.compile(r"[^a-z']+", _FLAGS)
NAME_STOP = {'the', 'and', 'years', 'year', 'old', 'currently', 'from', 'here', 'working', 'work', 'your',
             'you', 'call', 'sir', 'maam', 'just', 'also', 'been', 'have', 'graduate', 'graduated', 'newly',
             'hired', 'manager', 'restaurant', 'assistant', 'supervisor', 'candidate', 'number'}

CARRY_SECONDS = 20        # the same voice going on within 20 s keeps its seat
CALL_ANSWER_SECONDS = 30  # the first answer within 30 s of a panel call is that seat


def number_of(word):
    """'four' / '4' / '04' -> 4; anything else (including 0) -> None, like JS `NUMW[w]||+w||null`."""
    word = str(word).lower()
    if word in NUMBER_WORDS:
        return NUMBER_WORDS[word]
    return int(word) if word.isdigit() and int(word) else None


def _low(text) -> str:
    return re.sub(r'[“”"]', '', str(text or '').lower()).strip()


# ── Single-line cues ─────────────────────────────────────────────────────────
def self_cue(text):
    """The speaker states their own seat number, else None."""
    t = _low(text)
    for rx in _SELF_CUES:
        m = rx.search(t)
        if m:
            return number_of(m.group(1))
    m = _LEAD.search(t)
    return number_of(m.group(1)) if m else None


def call_cue(text):
    """The panel addressing exactly one seat ("Number five, I want you to start")."""
    t = _low(text)
    if self_cue(t):
        return None
    found = list(_CALL.finditer(t))
    return number_of(found[0].group(1)) if len(found) == 1 else None


def levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if not m or not n:
        return max(m, n)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i]
        for j in range(1, n + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1)))
        prev = cur
    return prev[n]


def name_cue(text, cands: list):
    """"My name is Rose Ancineta" -> the seat whose roster name matches best.
    Whisper spells names in many ways, so near matches count, but the winner
    must be clearly ahead of the runner-up."""
    t = _low(text)
    m = _NAME_INTRO.search(t)
    if not m:
        return None
    words = [w for w in _NON_NAME.split(m.group(1)) if len(w) >= 3 and w not in NAME_STOP][:4]
    if not words:
        return None
    scores = []
    for cand in cands:
        tokens = [k for k in _NON_NAME.split(str(cand.get('name') or '').lower()) if len(k) >= 3]
        best = 0.0
        for w in words:
            for k in tokens:
                s = 1 - levenshtein(w, k) / max(len(w), len(k))
                if min(len(w), len(k)) >= 4 and (w in k or k in w):
                    s = max(s, 0.85)
                if s > best:
                    best = s
        scores.append((cand['no'], best))
    scores.sort(key=lambda item: -item[1])          # stable, like Array.prototype.sort
    if scores and scores[0][1] >= 0.78 and (len(scores) < 2 or scores[0][1] - scores[1][1] >= 0.12):
        return scores[0][0]
    return None


def cue_of(text, cands: list, seat_numbers=None):
    """What one line says about who is talking: ('num'|'name'|'call', seat) or None."""
    seat_numbers = seat_numbers if seat_numbers is not None else [c['no'] for c in cands]
    n = self_cue(text)
    if n and n in seat_numbers:
        return ('num', n)
    named = name_cue(text, cands)
    if named:
        return ('name', named)
    called = call_cue(text)
    if called and called in seat_numbers:
        return ('call', called)
    return None


def normalize_roster(cands) -> list:
    """[{no, name}] from a list of {no, name} or a {seat: name} mapping; seat numbers as ints."""
    if isinstance(cands, dict):
        cands = [{'no': no, 'name': name} for no, name in cands.items()]
    out = []
    for cand in cands or []:
        try:
            no = int(cand['no'])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({'no': no, 'name': str(cand.get('name') or '')})
    return out


# ── Speaker label -> seat (only when speakers were separated offline) ────────
def auto_map(segs: list, cands: list) -> dict:
    """Vote each diarized speaker label onto a seat number or 'panel'. segs are
    {'a': start, 'z': end, 't': text, 's': speaker label or ''}."""
    cues = [cue_of(g['t'], cands) for g in segs]
    votes, talk = {}, {}

    def vote(speaker, key, weight):
        box = votes.setdefault(speaker, {})
        box[key] = box.get(key, 0) + weight

    for i, g in enumerate(segs):
        if not g['s']:
            continue
        talk[g['s']] = talk.get(g['s'], 0) + (g['z'] - g['a'])
        cue = cues[i]
        if not cue:
            continue
        if cue[0] == 'call':
            vote(g['s'], 'panel', 1)
            nxt = next((x for x in segs[i + 1:i + 4] if x['s'] and x['s'] != g['s'] and x['a'] - g['z'] < 15), None)
            if nxt:
                vote(nxt['s'], cue[1], 0.5)
        else:
            vote(g['s'], cue[1], 1)

    mapping = {}
    for speaker, box in votes.items():
        # JS objects list integer keys first, ascending: keep that order so ties break the same way.
        seats = sorted(((k, v) for k, v in box.items() if k != 'panel'), key=lambda kv: kv[0])
        seat_votes, panel_votes = sum(v for _, v in seats), box.get('panel', 0)
        if panel_votes >= 2 and panel_votes > seat_votes:
            mapping[speaker] = 'panel'
            continue
        if not seats:
            continue
        best = sorted(seats, key=lambda kv: -kv[1])[0]
        if best[1] >= 1 and best[1] / seat_votes >= 0.6 and best[1] > panel_votes:
            mapping[speaker] = best[0]
    # The biggest talker with no seat evidence is the panel, if nobody is yet.
    unclaimed = [(sp, secs) for sp, secs in talk.items()
                 if mapping.get(sp) is None and not any(k != 'panel' for k in votes.get(sp, {}))]
    unclaimed.sort(key=lambda kv: -kv[1])
    if unclaimed and 'panel' not in mapping.values():
        mapping[unclaimed[0][0]] = 'panel'
    return mapping


# ── Per-line tagging (forward only, so it also runs live) ────────────────────
class SeatTagger:
    """Tags lines one at a time. Every decision depends only on earlier lines,
    so tagging a live transcript line by line gives exactly the same tags as
    tagging the finished transcript in one go (tag_segments below)."""

    def __init__(self, cands, speaker_map: dict = None, overrides: dict = None):
        self.cands = normalize_roster(cands)
        self.seat_numbers = [c['no'] for c in self.cands]
        self.speaker_map = speaker_map or {}
        self.overrides = overrides or {}
        self.index = 0
        self._current, self._current_at, self._current_speaker = None, -1e9, ''
        self._call = None                         # {'n', 's', 'at'} after the panel called a seat

    def tag(self, start: float, end: float, text: str, speaker: str = '') -> tuple:
        """Returns (seat number | 'panel' | None, how) where how is 'set', 'speaker',
        'num', 'name', 'call', 'called' (first answer after a call) or 'carry'."""
        i, self.index = self.index, self.index + 1
        speaker = speaker or ''
        cue = cue_of(text, self.cands, self.seat_numbers)
        n, how = None, ''
        over = self.overrides.get(i)
        mapped = self.speaker_map.get(speaker) if speaker else None
        if over is not None and over != '':
            n, how = ('panel' if over == 'panel' else int(over)), 'set'
        elif mapped is not None and mapped != '':
            n, how = ('panel' if mapped == 'panel' else int(mapped)), 'speaker'
        elif cue and cue[0] != 'call':
            n, how = cue[1], cue[0]
        elif cue and cue[0] == 'call':
            n, how = 'panel', 'call'
        elif self._call and start - self._call['at'] <= CALL_ANSWER_SECONDS and \
                (not speaker or speaker != self._call['s']):
            n, how = self._call['n'], 'called'
        elif self._current is not None and start - self._current_at <= CARRY_SECONDS and \
                (not speaker or speaker == self._current_speaker):
            n, how = self._current, 'carry'
        if cue and cue[0] == 'call' and how != 'set':
            self._call = {'n': cue[1], 's': speaker, 'at': end}
        elif isinstance(n, int):
            self._call = None
        if isinstance(n, int):
            self._current, self._current_at, self._current_speaker = n, end, speaker
        elif n == 'panel':
            self._current = None
        return n, how


def tag_segments(segs: list, cands, speaker_map: dict = None, overrides: dict = None) -> list:
    """Whole-transcript tagging, same as the JS tagSegs: [{'n', 'how'}] per seg."""
    tagger = SeatTagger(cands, speaker_map, overrides)
    return [dict(zip(('n', 'how'), tagger.tag(g['a'], g['z'], g['t'], g.get('s') or ''))) for g in segs]


def santa_segments(transcript: dict) -> list:
    """Santa .json segments -> the tagger's {a, z, t, s} lines (empty lines dropped), as parseSanta does."""
    out = []
    for x in transcript.get('segments') or []:
        text = str(x.get('text') or '').strip()
        if text:
            out.append({'a': round(float(x.get('start') or 0), 2), 'z': round(float(x.get('end') or 0), 2),
                        't': text, 's': str(x['speaker']) if x.get('speaker') else ''})
    return out
