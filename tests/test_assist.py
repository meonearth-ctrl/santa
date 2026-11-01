# tests/test_assist.py
# Live Assist: when probe calls fire (the prototype's eventsOf rules, fed line
# by line), the append-only conversations, suggestion cards (labels, replaced,
# stale), activity-driven ratings, budgets, the <label>.assist.json output and
# who may see ratings over HTTP. Claude is replaced by a fake client, so these
# run offline and cost nothing. Prompt text equals the Node prototypes; that is
# checked against the real Hiring Right config when it is on this Mac.

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from whisper_key.santa import assist, assist_prompts as ap

HR_CONFIG = Path.home() / 'Documents/Hiring Right/09_Live_Replay'
ROSTER = [{'no': 1, 'name': 'Rose Ancineta'}, {'no': 2, 'name': 'Mark Cruz'}, {'no': 3, 'name': 'Ana Santos'}]


# ── A small made-up config in Hiring Right's shape ───────────────────────────
def tiny_config() -> dict:
    comp = lambda key, w: {'key': key, 'name': key.title(), 'w': w, 'a1': 'weak', 'a3': 'ok', 'a5': 'strong'}
    fw = {'competencies': [comp('empathy', 2), comp('clarity', 1)]}
    bm = {'version': 'b1', 'strong': 4, 'hire': 3.2, 'hold': 2.8, 'core': ['empathy'], 'coreMin': 3,
          'exerciseRules': {'presenceVideo': 'x', 'roleplay': 'y'}}
    run = {'run': [{'id': 't1', 'name': 'Welcome', 'type': 'briefing', 'min': 3},
                   {'id': 't2', 'name': 'Intros', 'type': 'intro', 'min': 6},
                   {'id': 't3', 'name': 'Open round', 'type': 'individual', 'min': 6}]}
    return {'framework': fw, 'framework_manager': fw, 'benchmarks': bm, 'benchmarks_manager': bm,
            'brands': {'list': ['Pizza Hut', 'IHOP']}, 'activities': run, 'activities_manager': run,
            'bank': [{'id': 'pq_empathy.1', 'track': 'team', 'comps': ['empathy'], 'text': 'Tell us about a guest you helped.'}]}


SETTINGS = {'assist_probe_model': 'claude-sonnet-5-5', 'assist_rating_model': 'claude-sonnet-5-5',
            'assist_session_budget': 2, 'assist_daily_budget': 40}


class FakeClient:
    """Stands in for anthropic.Anthropic: records requests, answers from a script."""

    def __init__(self, probe_reply=None, rating_reply=None, fail=False):
        self.requests, self.fail = [], fail
        self.probe_reply = probe_reply or {'probes': [{'seat': 2, 'probe_id': 'pq_empathy.1', 'say': 'Tell us more.',
                                                       'competency': 'empathy', 'why': 'No empathy evidence.'}],
                                           'gaps': [{'seat': 2, 'missing': ['clarity']}], 'quiet': [3]}
        self.rating_reply = rating_reply or {'seats': [
            {'seat': 2, 'ratings': [{'key': 'empathy', 's': 4, 'ev': 'Helped a guest'}, {'key': 'clarity', 's': 3, 'ev': 'Not yet seen'}],
             'rec': 'Hire', 'level': '', 'brand': 'IHOP', 'integrity': 4, 'flags': [], 'evidence': 'medium',
             'oneLine': 'Warm.', 'standout': 'Empathy', 'concern': 'None', 'probe': 'Check clarity'}]}
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=str)))
        if self.fail:
            raise RuntimeError('network down')
        reply = self.rating_reply if 'seats' in json.dumps(request['output_config']) else self.probe_reply
        block = SimpleNamespace(type='text', text=json.dumps(reply), to_dict=lambda: {'type': 'text', 'text': json.dumps(reply)})
        usage = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 4000, 'cache_creation_input_tokens': 0}
        return SimpleNamespace(content=[block], stop_reason='end_turn', model=request['model'],
                               usage=SimpleNamespace(to_dict=lambda: usage))


def line(start, end, text, seat=None):
    seg = {'start': start, 'end': end, 'text': text}
    if seat is not None:
        seg['seat'] = seat
    return seg


def wait_for(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ── Trigger rules (replay.mjs eventsOf) ──────────────────────────────────────
class ProbeTriggerTests(unittest.TestCase):
    def feed(self, lines, trigger=None):
        trigger = trigger or assist.ProbeTrigger()
        return [e for e in (trigger.line(seg) for seg in lines) if e]

    def test_answer_ends_when_someone_else_speaks(self):
        events = self.feed([line(0, 6, 'a', 2), line(6, 13, 'b', 2), line(14, 16, 'next?', 'panel')])
        self.assertEqual(events, [{'at': 15.0, 'seat': 2}])

    def test_short_answers_and_cooldown(self):
        events = self.feed([line(0, 5, 'short', 1), line(6, 20, 'long', 2), line(21, 35, 'long', 3),
                            line(36, 37, 'ok', 'panel'), line(80, 95, 'long', 1), line(96, 97, 'ok', 'panel')])
        # seat 1's 5 s answer doesn't count; seat 3 is inside seat 2's 45 s cooldown.
        self.assertEqual(events, [{'at': 22.0, 'seat': 2}, {'at': 97.0, 'seat': 1}])

    def test_untagged_lines_inside_an_answer_dont_end_it(self):
        events = self.feed([line(0, 8, 'a', 2), line(9, 11, 'b'), line(11.5, 16, 'c', 2), line(30, 31, 'later')])
        self.assertEqual(events, [{'at': 18.0, 'seat': 2}])

    def test_silence_fires_from_the_clock(self):
        trigger = assist.ProbeTrigger()
        self.assertEqual(self.feed([line(0, 13, 'a', 2)], trigger), [])
        self.assertIsNone(trigger.tick(19))
        self.assertEqual(trigger.tick(20.5), {'at': 15.0, 'seat': 2})


# ── Prompt pieces ────────────────────────────────────────────────────────────
class PromptTests(unittest.TestCase):
    def test_lines_and_heads(self):
        self.assertEqual(ap.line_of(line(75.4, 80, 'Hello', 3)), '[1:15] #3: Hello')
        self.assertEqual(ap.line_of(line(5, 6, 'Next', 'panel')), '[0:05] Panel: Next')
        self.assertEqual(ap.line_of(line(5, 6, 'Hm')), '[0:05] ?: Hm')
        self.assertEqual(ap.group_head('G4', 'team', ROSTER[:2]), 'Group G4 (team). Seats: #1 Rose Ancineta, #2 Mark Cruz.\n\n')

    def test_rating_prompt_drops_presence_rules_and_prints_numbers_like_js(self):
        text = ap.rating_system(tiny_config(), 'team')
        self.assertIn('(weight 2)', text)
        self.assertIn('"roleplay": "y"', text)
        self.assertNotIn('presenceVideo', text)
        self.assertIn('brand: best-fit brand from ["Pizza Hut","IHOP"]', text)

    def test_cost(self):
        usage = {'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cache_read_input_tokens': 1_000_000,
                 'cache_creation_input_tokens': 1_000_000}
        self.assertAlmostEqual(ap.cost_of('claude-sonnet-5-5', usage), 2 + 1 + 0.2 + 2.5)

    @unittest.skipUnless(shutil.which('node') and (HR_CONFIG / 'replay.mjs').exists(),
                         'Hiring Right prototypes are only on the owner Mac')
    def test_prompts_equal_the_node_prototypes(self):
        script = """
const fs = require('fs'); process.chdir(process.argv[1]);
const load = (f) => { const d = JSON.parse(fs.readFileSync(f, 'utf8')); return d.data || d; };
const cut = (src, a, b) => src.slice(src.indexOf(a), src.indexOf(b)).replace(/^const /gm, 'var ');
const rp = fs.readFileSync('replay.mjs', 'utf8'), rt = fs.readFileSync('rate.mjs', 'utf8'); const out = {};
{ const TIMING = 'hybrid'; const FW = { team: load('config/framework.json'), manager: load('config/framework_manager.json') };
  const BANK = load('probe-bank.json'); eval(cut(rp, 'function systemFor', '/* ---------- transcript'));
  out.probe = { team: systemFor('team'), manager: systemFor('manager') }; out.probeSchema = SCHEMA; }
{ const FW = { team: load('config/framework.json'), manager: load('config/framework_manager.json') };
  const BM = { team: load('config/benchmarks.json'), manager: load('config/benchmarks_manager.json') };
  const BRANDS = load('config/brands.json'); const RECS = ['Strong hire', 'Hire', 'Hold', 'No hire'];
  eval(cut(rt, 'function systemFor', '/* ---------- replay'));
  out.rate = { team: systemFor('team'), manager: systemFor('manager') }; out.rateSchema = { team: schemaFor('team'), manager: schemaFor('manager') }; }
process.stdout.write(JSON.stringify(out));
"""
        js = json.loads(subprocess.run(['node', '-e', script, str(HR_CONFIG)], capture_output=True, text=True,
                                       timeout=60, check=True).stdout)
        cfg = ap.load_config(str(HR_CONFIG))
        self.assertEqual(ap.PROBE_SCHEMA, js['probeSchema'])
        for track in ('team', 'manager'):
            self.assertEqual(ap.probe_system(cfg, track), js['probe'][track])
            self.assertEqual(ap.rating_system(cfg, track), js['rate'][track])
            self.assertEqual(ap.rating_schema(cfg, track), js['rateSchema'][track])


# ── A whole session with a fake Claude ───────────────────────────────────────
class AssistSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def session(self, client=None, settings=None):
        self.client = client or FakeClient()
        return assist.AssistSession('G4', 'team', ROSTER, tiny_config(), self.client, dict(SETTINGS, **(settings or {})),
                                    assist.SpendLedger(self.tmp.name), self.tmp.name, 'G4.wav')

    def test_probe_call_shape_cards_and_append_only_conversation(self):
        a = self.session()
        a.add_lines([line(0, 3, 'Number two, introduce yourself.', 'panel'), line(4, 18, 'I worked at Jollibee.', 2),
                     line(19, 20, 'Thanks.', 'panel')], 20)
        self.assertTrue(wait_for(lambda: a.view(False)['cards']))
        req = self.client.requests[0]
        self.assertEqual(req['model'], 'claude-sonnet-5-5')
        self.assertEqual(req['fallbacks'], 'default')
        self.assertEqual(req['betas'], ['server-side-fallback-2026-07-01'])
        self.assertEqual(req['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(req['system'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(req['output_config']['effort'], 'low')
        self.assertEqual(req['output_config']['format']['schema'], ap.PROBE_SCHEMA)
        self.assertEqual(req['messages'][0]['content'],
                         'Group G4 (team). Seats: #1 Rose Ancineta, #2 Mark Cruz, #3 Ana Santos.\n\n'
                         '[0:00] Panel: Number two, introduce yourself.\n[0:04] #2: I worked at Jollibee.\n\n'
                         '[0:20] #2 has just finished an answer. What should the panel ask next?')
        view = a.view(False)
        self.assertEqual([(c['seat'], c['label'], c['say']) for c in view['cards']], [(2, 'now', 'Tell us more.')])
        self.assertEqual(view['gaps'], [{'seat': 2, 'missing': ['clarity']}])
        self.assertNotIn('rating', view)
        # The next call repeats the history unchanged and adds only the new lines.
        a.add_lines([line(70, 86, 'Long answer.', 3), line(87, 88, 'Ok.', 'panel')], 88)
        self.assertTrue(wait_for(lambda: len(self.client.requests) == 2))
        second = self.client.requests[1]['messages']
        self.assertEqual(second[:2], [self.client.requests[0]['messages'][0], second[1]])
        self.assertEqual(second[1]['role'], 'assistant')
        self.assertTrue(second[2]['content'].startswith('[0:19] Panel: Thanks.\n[1:10] #3: Long answer.'))

    def test_card_labels_replacement_staleness_and_status(self):
        a = self.session()
        a.activity('t2', 'start')                                   # intros: "ask after this activity"
        a.add_lines([line(0, 14, 'a', 2), line(15, 16, 'b', 'panel')], 16)
        self.assertTrue(wait_for(lambda: a.view(False)['cards']))
        first = a.view(False)['cards'][0]
        self.assertEqual(first['label'], 'after_activity')
        a.add_lines([line(60, 75, 'c', 2), line(76, 77, 'd', 'panel')], 77)   # newer card for seat 2
        self.assertTrue(wait_for(lambda: len(a.cards) == 2))
        cards = a.view(False)['cards']
        self.assertEqual(len(cards), 1)
        self.assertEqual(a.cards[0]['status'], 'dropped')
        self.assertTrue(a.mark_card(cards[0]['id'], 'asked'))
        self.assertEqual(a.view(False)['cards'], [])
        a.add_lines([line(130, 145, 'e', 2), line(146, 147, 'f', 'panel')], 147)
        self.assertTrue(wait_for(lambda: len(a.cards) == 3))
        a.tick(147 + assist.STALE + 10)
        self.assertEqual(a.view(False)['cards'], [])               # older than 6 minutes: dropped

    def test_activity_end_rates_then_final_and_output_file(self):
        a = self.session()
        a.activity('t2', 'start')
        a.add_lines([line(1, 20, 'My name is Mark.', 2)], 20)
        a.tick(100)
        a.activity('t2', 'finish')                                  # 99 s: too short to rate
        a.activity('t3', 'start')
        a.add_lines([line(101, 130, 'I calmed an angry guest.', 2)], 130)
        a.tick(260)
        a.activity('t3', 'finish')                                  # 160 s: rated
        self.assertTrue(wait_for(lambda: a.ratings))
        rating_req = next(r for r in self.client.requests if r['max_tokens'] == assist.RATING_MAX_TOKENS)
        self.assertEqual(rating_req['output_config']['effort'], 'medium')
        self.assertTrue(rating_req['messages'][0]['content'].endswith(
            '[4:20] The activity "Open round" (individual) has just ended. Re-score every seat.'))
        self.assertEqual(a.view(True)['rating'][0]['overall'], 3.67)
        a.finish(300)
        data = json.loads(Path(self.tmp.name, 'G4.assist.json').read_text(encoding='utf-8'))
        self.assertEqual((data['kind'], data['version'], data['label'], data['track'], data['source']),
                         ('hiring-right-live-assist', 1, 'G4', 'team', 'G4.wav'))
        self.assertEqual([h['trigger'] for h in data['history']], ['activity_end', 'final'])
        self.assertEqual(data['final'][0]['candNo'], 2)
        self.assertEqual(data['final'][0]['ratings']['empathy'], {'s': 4, 'ev': 'Helped a guest'})
        self.assertEqual(data['final'][0]['brandRec'], 'IHOP')
        self.assertEqual([x['name'] for x in data['activities']], ['Intros', 'Open round'])
        self.assertEqual(data['activities'][1], {'name': 'Open round', 'type': 'individual', 'start': 100.0, 'end': 260.0})
        self.assertAlmostEqual(data['cost']['total'], data['cost']['probes'] + data['cost']['rating'], places=4)
        self.assertTrue(all('usage' in c and 'cost' in c for c in data['calls']))
        self.assertNotIn('sk-', json.dumps(data))
        # The last rating call asks for the final rating.
        self.assertIn('This is your final live rating.', self.client.requests[-1]['messages'][-1]['content'])

    def test_budget_stops_calls(self):
        a = self.session(settings={'assist_session_budget': 0})
        a.add_lines([line(0, 14, 'a', 2), line(15, 16, 'b', 'panel')], 16)
        self.assertTrue(wait_for(lambda: a.view(False)['capped']))
        self.assertEqual(self.client.requests, [])
        self.assertIn('Session budget', a.view(False)['capped'])

    def test_failed_call_keeps_its_lines_for_the_next_one(self):
        a = self.session(client=FakeClient(fail=True))
        a.add_lines([line(0, 14, 'first answer', 2), line(15, 16, 'b', 'panel')], 16)
        self.assertTrue(wait_for(lambda: a.view(False)['error']))
        self.assertIn('first answer', ''.join(seg['text'] for seg in a._unsent_probe))
        self.assertEqual(a.probes.messages, [])                    # the failed turn was taken back out


# ── HTTP: phones see cards; only a lead phone may switch ratings on ──────────
try:
    import faster_whisper  # noqa: F401
    HAVE_FW = True
except ImportError:
    HAVE_FW = False


@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed by the service)')
class AssistServerTests(unittest.TestCase):
    def setUp(self):
        from test_santa import make_service, FakeTranscriber
        from whisper_key.santa import live
        from whisper_key.santa.server import make_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = make_service(self.tmp.name, FakeTranscriber(text='x'))
        self.svc.settings.update({'export_dir': os.path.join(self.tmp.name, 'out')})
        self.svc.live = live.LiveManager(self.svc, os.path.join(self.tmp.name, 'live'))
        self.httpd = make_server('127.0.0.1', 0, self.svc)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method, headers={'X-Santa': '1'})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_assist_endpoints_from_the_mac(self):
        self.assertEqual(self.req('GET', '/api/assist'), (200, {'active': False}))
        self.assertEqual(self.req('POST', '/api/assist/card', {'id': 'x', 'status': 'asked'})[0], 409)
        a = assist.AssistSession('G9', 'team', ROSTER, tiny_config(), FakeClient(), SETTINGS,
                                 assist.SpendLedger(self.tmp.name), os.path.join(self.tmp.name, 'out'), 'G9.wav')
        self.svc.live.current = SimpleNamespace(assist=a)
        status, view = self.req('GET', '/api/assist')
        self.assertEqual((status, view['active'], view['can_rate'], view['show_rating']), (200, True, True, False))
        self.assertNotIn('rating', view)
        status, view = self.req('POST', '/api/assist/activity', {'id': 't2', 'action': 'start'})
        self.assertEqual([s['status'] for s in view['steps']], ['pending', 'running', 'pending'])
        self.assertEqual(self.req('POST', '/api/assist/activity', {'id': 'nope', 'action': 'start'})[0], 400)
        status, view = self.req('POST', '/api/assist/show-rating', {'on': True})
        self.assertTrue(view['show_rating'])
        self.assertEqual(view['rating'], [])


class LeadDeviceTests(unittest.TestCase):
    def test_lead_flag_is_stored_and_public(self):
        from whisper_key.santa.phone import DeviceStore
        with tempfile.TemporaryDirectory() as home:
            store = DeviceStore(home)
            dev = store.create_token('Lead phone')
            self.assertFalse(store.verify(dev['token'])['lead'])
            self.assertTrue(store.set_lead(dev['device_id'], True))
            self.assertTrue(DeviceStore(home).verify(dev['token'])['lead'])
            self.assertFalse(store.set_lead('missing', True))


if __name__ == '__main__':
    unittest.main()
