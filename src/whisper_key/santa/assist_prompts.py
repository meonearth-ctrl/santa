# santa/assist_prompts.py
# What Live Assist tells Claude: the system prompts, output schemas and cue lines,
# ported word for word from Hiring Right's tested Node prototypes
# (09_Live_Replay/replay.mjs "hybrid" run for probes, rate.mjs for live rating).
# Keep them identical to the prototypes: the measured cost, latency and quality
# figures in LIVE_ASSIST_SPEC.md belong to exactly these texts. The system
# prompts are the same for every group of a track, so they cache.

import json
import os

# $ per 1M tokens: input, output, cache read; cache writes (5-minute) cost 1.25x input
PRICES = {'claude-sonnet-5-5': (2, 10, 0.2), 'claude-opus-5-5': (4, 20, 0.2), 'claude-haiku-5-5': (0.1, 0.5, 0.01)}
RECS = ['Strong hire', 'Hire', 'Hold', 'No hire']
CONFIG_FILES = ('framework', 'framework_manager', 'benchmarks', 'benchmarks_manager', 'brands',
                'activities', 'activities_manager')


# ── Hiring Right config (refreshed from the app into 09_Live_Replay) ─────────
def _load(path: str):
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    return data['data'] if isinstance(data, dict) and 'data' in data else data


def load_config(folder: str) -> dict:
    """{'framework': {...}, ..., 'bank': [...]} from <folder>/config/*.json and probe-bank.json."""
    folder = os.path.expanduser(folder)
    cfg = {name: _load(os.path.join(folder, 'config', name + '.json')) for name in CONFIG_FILES}
    cfg['bank'] = _load(os.path.join(folder, 'probe-bank.json'))
    return cfg


def framework(cfg: dict, track: str) -> dict:
    return cfg['framework_manager' if track == 'manager' else 'framework']


def run_steps(cfg: dict, track: str) -> list:
    """The interview's run steps for the track (id, name, type, minutes) for the activity strip."""
    acts = cfg['activities_manager' if track == 'manager' else 'activities']
    return [{'id': r.get('id'), 'name': r.get('name'), 'type': r.get('type'), 'min': r.get('min')}
            for r in acts.get('run') or []]


def _who(track: str) -> str:
    return 'restaurant manager' if track == 'manager' else 'front-of-house crew (Guest Experience Creator)'


# ── Probes (replay.mjs systemFor with TIMING = "hybrid", SCHEMA) ─────────────
def probe_system(cfg: dict, track: str) -> str:
    comps = '\n'.join(f"- {c['key']} · {c['name']}: 1 = {c['a1']} | 3 = {c['a3']} | 5 = {c['a5']}"
                      for c in framework(cfg, track)['competencies'])
    probes = '\n'.join(f"{p['id']} | {','.join(p['comps']) or '-'} | {p['text']}"
                       for p in cfg['bank'] if p.get('track') == track)
    return f"""You assist a hiring panel at Kout Food Group (Kuwait restaurants) during a live group interview of {_who(track)} candidates in the Philippines. Candidates wear seat numbers. You receive the live transcript in pieces, from local speech recognition: it has errors, and seat tags (#N) can be wrong or missing ("?").

Each time a candidate finishes an answer, suggest what the panel could ask next.
- Choose from APPROVED PROBES only, by id. You may adapt the wording slightly to fit what was just said (same meaning, same competency); put the words to say in "say".
- At most 2 probes. Prefer, in order: a competency with no clear evidence yet for that seat; a seat that has spoken least; a follow-up that tests a specific claim the candidate just made.
- Never repeat a probe already asked to that seat. Never suggest anything about age, family, marital status, religion, nationality, health, pregnancy or appearance. Accent and grammar are never assessed.
- If nothing useful can be added now, return an empty "probes" list.
- Also list, per seat present, the competency keys with no clear evidence yet ("gaps"), and the seats that have barely spoken ("quiet").
The panel decides. Keep "why" under 20 words.
- The panel may see your suggestion a few minutes later, when the current activity ends, so phrase it to still make sense then.
- Refer to or quote what a candidate said only when that line is tagged with their seat (#N) in the transcript. If the speaker is "?" or another seat, do not attribute it; ask without a premise instead.

COMPETENCIES (key · name: anchors)
{comps}

APPROVED PROBES (id | competencies | question)
{probes}"""


PROBE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['probes', 'gaps', 'quiet'],
    'properties': {
        'probes': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['seat', 'probe_id', 'say', 'competency', 'why'],
            'properties': {'seat': {'type': 'integer'}, 'probe_id': {'type': 'string'}, 'say': {'type': 'string'},
                           'competency': {'type': 'string'}, 'why': {'type': 'string'}}}},
        'gaps': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['seat', 'missing'],
            'properties': {'seat': {'type': 'integer'}, 'missing': {'type': 'array', 'items': {'type': 'string'}}}}},
        'quiet': {'type': 'array', 'items': {'type': 'integer'}},
    },
}


# ── Live rating (rate.mjs systemFor, schemaFor) ──────────────────────────────
def rating_system(cfg: dict, track: str) -> str:
    fw = framework(cfg, track)
    bm = cfg['benchmarks_manager' if track == 'manager' else 'benchmarks']
    comps = '\n'.join(f"- {c['key']} · {c['name']} (weight {_js_num(c['w'])}): 1 = {c['a1']} | 3 = {c['a3']} | 5 = {c['a5']}"
                      for c in fw['competencies'])
    rules = bm.get('exerciseRules')
    # Same keys and order as the JS object literal. JSON.stringify leaves out keys
    # that are undefined (missing here) but keeps ones that are null.
    bench = {k: bm[k] for k in ('version', 'strong', 'hire', 'hold', 'core', 'coreMin', 'floors', 'hardFlags', 'levels')
             if k in bm}
    if rules:
        bench['rules'] = {k: v for k, v in rules.items() if 'presence' not in k.lower()}
    bench.update({k: bm[k] for k in ('stepDownRules', 'learned') if k in bm})
    bench = json.dumps(_js_numbers(bench), indent=1, ensure_ascii=False)
    extra = ', manager levels and step-down rules' if track == 'manager' else ''
    if track == 'manager':
        fit = 'level: the manager level they meet (or "").'
    else:
        brands = json.dumps(cfg['brands'].get('list') or [], ensure_ascii=False, separators=(',', ':'))
        fit = f'brand: best-fit brand from {brands} for Hire or Strong hire, else "".'
    return f"""You are the third rater on a hiring panel at Kout Food Group (Kuwait restaurants), scoring a live group interview of {_who(track)} candidates in the Philippines. You get the live transcript in pieces from local speech recognition: it has errors, and seat tags (#N) can be wrong or missing ("?"). You do not see video or CVs.

After each activity, re-score every seat on ALL the evidence so far (not just the latest piece):
- Score each competency 1–5 in steps of 0.5 against its anchors. With no real evidence yet, give 3 and say "Not yet seen" as the evidence; do not guess.
- Evidence ("ev"): under 12 words, what the candidate said or did, with the time if you can. Attribute something to a seat only when the line is tagged with that seat; otherwise leave it out.
- Recommendation: apply the benchmark below (bands on the weighted score, core minimums, floors, hard flags{extra}). Presence is read from video by the panel, so ignore presence rules. "evidence" says how much you have seen of this seat: low, medium or high.
- {fit}
- Never use age, family, marital status, religion, nationality, health, appearance, accent or grammar. Integrity 1–5 from what was said (honesty, safety, ownership).
- oneLine, standout, concern, probe (what the panel should check before deciding): each under 25 words.
The panel decides; you give evidence.

COMPETENCIES
{comps}

BENCHMARK ({bm.get('version')})
{bench}"""


def rating_schema(cfg: dict, track: str) -> dict:
    keys = [c['key'] for c in framework(cfg, track)['competencies']]
    return {'type': 'object', 'additionalProperties': False, 'required': ['seats'], 'properties': {'seats': {
        'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['seat', 'ratings', 'rec', 'level', 'brand', 'integrity', 'flags', 'evidence', 'oneLine',
                         'standout', 'concern', 'probe'],
            'properties': {
                'seat': {'type': 'integer'},
                'ratings': {'type': 'array', 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['key', 's', 'ev'],
                    'properties': {'key': {'type': 'string', 'enum': keys}, 's': {'type': 'number'},
                                   'ev': {'type': 'string'}}}},
                'rec': {'type': 'string', 'enum': RECS}, 'level': {'type': 'string'}, 'brand': {'type': 'string'},
                'integrity': {'type': 'number'}, 'flags': {'type': 'array', 'items': {'type': 'string'}},
                'evidence': {'type': 'string', 'enum': ['low', 'medium', 'high']}, 'oneLine': {'type': 'string'},
                'standout': {'type': 'string'}, 'concern': {'type': 'string'}, 'probe': {'type': 'string'}}}}}}


def weighted_overall(cfg: dict, track: str, ratings: dict):
    """rate-report.mjs `weighted`: competency weights over the scores present."""
    total = weight = 0.0
    for c in framework(cfg, track)['competencies']:
        value = ratings.get(c['key'])
        if value:
            total += value * c['w']
            weight += c['w']
    return round(total / weight, 2) if weight else None


# ── Transcript lines and cues ────────────────────────────────────────────────
def mmss(seconds: float) -> str:
    return f'{int(seconds // 60)}:{int(seconds % 60):02d}'


def line_of(seg: dict) -> str:
    """'[m:ss] #3: text' — the tag the tagger gave the line: #N, Panel or '?'."""
    seat = seg.get('seat')
    who = f'#{seat}' if isinstance(seat, int) else 'Panel' if seat == 'panel' else '?'
    return f"[{mmss(seg['start'])}] {who}: {seg['text']}"


def group_head(label: str, track: str, roster: list) -> str:
    seats = ', '.join(f"#{c['no']} {c['name']}" for c in roster)
    return f'Group {label} ({track}). Seats: {seats}.\n\n'


def cost_of(model: str, usage) -> float:
    pi, po, pr = PRICES.get(model, PRICES['claude-sonnet-5-5'])
    get = (lambda k: getattr(usage, k, None) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k) or 0)
    return (get('input_tokens') * pi + get('cache_creation_input_tokens') * pi * 1.25
            + get('cache_read_input_tokens') * pr + get('output_tokens') * po) / 1e6


def _js_num(value) -> str:
    """Numbers as JavaScript prints them in a template string (1.0 -> '1')."""
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _js_numbers(value):
    """Whole floats as ints, so json.dumps prints numbers like JSON.stringify (3.0 -> 3)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _js_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_js_numbers(v) for v in value]
    return value
