# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline bootstrap rollouts over several repositories: canary first, then staggered [skip ci] commits
(Cratis/Workflows#163). The only gh executable is a local stateful fake; SLEEP_CMD records waits instead."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / '.github/scripts/bootstrap-common-workflows.sh'
SKIP_CI = ' [skip ci]'
POLL, STAGGER, SETTLE, TIMEOUT = 3, 7, 9, 30

# Per-repository blob/tree/commit/ref state, plus a scripted sequence of workflow-run listings per repository.
FAKE = r'''#!/usr/bin/env python3
import base64, hashlib, json, os, sys
from pathlib import Path
p = Path(os.environ['STUB_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
method = a[a.index('-X')+1] if '-X' in a else 'GET'
endpoint = next(x for x in a if x.startswith('repos/'))
name = endpoint.split('/')[2]
s['calls'].append([name] + a)
r = s['repos'][name]
fields = dict(a[i+1].split('=',1) for i,x in enumerate(a) if x in ['-f','-F'])
body = json.load(sys.stdin) if '--input' in a else {}
error = None
result = None
def oid(x):
    return hashlib.sha1(json.dumps([name, x],sort_keys=True).encode()).hexdigest()
if method == 'GET' and '/actions/runs?' in endpoint and 'head_sha=' not in endpoint:
    # The pre-flight read of a canary's Actions runs.
    if name in s.get('actions_unreadable', []): error = 'HTTP 403 Resource not accessible by personal access token'
    else: result = {'total_count':0,'workflow_runs':[]}
elif method == 'GET' and '/actions/runs?' in endpoint:
    sha = endpoint.split('head_sha=')[1].split('&')[0]
    script = s['runs'].get(name, [[]])
    read = s['run_reads'].get(name, 0)
    s['run_reads'][name] = read + 1
    step = script[min(read, len(script)-1)]
    if step == 'error': error = 'HTTP 503 runs unavailable'
    else: result = {'total_count':len(step),'workflow_runs':[dict(run, head_sha=sha) for run in step]}
elif method == 'GET' and endpoint.count('/') == 2:
    result = {'default_branch':'main','permissions':{'push':True},'private':False}
elif '/git/ref/heads/' in endpoint:
    result = {'object':{'sha':r['head']}}
elif method == 'GET' and '/git/commits/' in endpoint:
    result = r['commits'][endpoint.split('/')[-1]]
elif method == 'GET' and '/git/trees/' in endpoint:
    result = {'tree':r['trees'][endpoint.split('/')[-1].split('?')[0]], 'truncated':False}
elif method == 'GET' and '/git/blobs/' in endpoint:
    result = {'content':r['blobs'][endpoint.split('/')[-1]]}
elif method == 'POST' and endpoint.endswith('/git/blobs'):
    data = base64.b64decode(fields['content'])
    sha = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    r['blobs'][sha] = fields['content']
    result = {'sha':sha}
elif method == 'POST' and endpoint.endswith('/git/trees'):
    entries = {e['path']:e for e in r['trees'][body['base_tree']]}
    for e in body['tree']:
        if e['sha'] is None: entries.pop(e['path'],None)
        else: entries[e['path']] = e
    sha = oid(sorted(entries))
    r['trees'][sha] = list(entries.values())
    result = {'sha':sha}
elif method == 'POST' and endpoint.endswith('/git/commits'):
    sha = oid(body)
    r['commits'][sha] = {'tree':{'sha':body['tree']},'parents':body['parents'],'message':body['message']}
    result = {'sha':sha}
elif method == 'PATCH' and '/git/refs/heads/' in endpoint:
    if r['commits'][fields['sha']]['parents'] != [r['head']]:
        error = 'Update is not a fast forward (HTTP 422)'
    else:
        r['head'] = fields['sha']
        s['events'].append('push ' + name)
        result = {'object':{'sha':r['head']}}
else:
    error = 'UNEXPECTED API CALL '+repr(a)
p.write_text(json.dumps(s))
if error:
    print(error,file=sys.stderr)
    sys.exit(1)
if '--jq' in a:
    q = a[a.index('--jq')+1]
    if q == '.permissions.push // false': print('true')
    elif q == '.default_branch': print(result['default_branch'])
    elif q == '.private': print('true' if result['private'] else 'false')
    elif q == '.content': print(result['content'])
    else: raise Exception(q)
else:
    print(json.dumps(result))
'''

SLEEPER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['STUB_STATE'])
s = json.loads(p.read_text())
s['events'].append('sleep ' + ' '.join(sys.argv[1:]))
p.write_text(json.dumps(s))
'''


def run(status, conclusion=None, name='Build'):
    return {'name': name, 'status': status, 'conclusion': conclusion}


class BootstrapRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bootstrap-rollout-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state_file = self.directory / 'state.json'
        for name, source in [('gh', FAKE), ('sleeper', SLEEPER)]:
            path = self.directory / name
            path.write_text(source)
            path.chmod(0o755)
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('CANARY_', 'STAGGER_'))}
        self.env = {**environment, 'PATH': str(self.directory) + os.pathsep + os.environ['PATH'],
                    'STUB_STATE': str(self.state_file), 'REPOS_FILE': str(self.directory / 'repos.json'),
                    'GITHUB_WORKSPACE': str(self.directory), 'REPOS_IGNORE': '[]', 'TMPDIR': str(self.directory),
                    'SLEEP_CMD': str(self.directory / 'sleeper'), 'CANARY_POLL_SECONDS': str(POLL),
                    'STAGGER_SECONDS': str(STAGGER), 'CANARY_SETTLE_SECONDS': str(SETTLE),
                    'CANARY_TIMEOUT_SECONDS': str(TIMEOUT)}

    def initialize(self, names, runs=None, up_to_date=(), actions_unreadable=()):
        repos = {name: {'head': 'a'*40, 'commits': {'a'*40: {'tree': {'sha': 'b'*40}, 'message': 'Initial'}},
                        'trees': {'b'*40: []}, 'blobs': {}} for name in names}
        self.state_file.write_text(json.dumps({'repos': repos, 'runs': {}, 'run_reads': {}, 'calls': [],
                                               'events': []}))
        if up_to_date:
            # Bring these repositories up to date with a real run, then forget that run.
            self.run_bootstrap(list(up_to_date), CANARY_SETTLE_SECONDS='0')
            state = json.loads(self.state_file.read_text())
            state.update({'runs': {}, 'run_reads': {}, 'calls': [], 'events': []})
            self.state_file.write_text(json.dumps(state))
        state = json.loads(self.state_file.read_text())
        state['runs'] = runs or {}
        state['actions_unreadable'] = list(actions_unreadable)
        self.state_file.write_text(json.dumps(state))

    def run_bootstrap(self, names, expected=0, **env):
        (self.directory / 'repos.json').write_text(json.dumps(names))
        result = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env={**self.env, **env}, text=True,
                                capture_output=True, timeout=120)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected, output)
        self.state = json.loads(self.state_file.read_text())
        return output

    def message(self, name):
        repo = self.state['repos'][name]
        return repo['commits'][repo['head']]['message']

    def touched(self, name):
        # Every call for the repository except the read-only pre-flight permission probe, which runs first.
        return [call for call in self.state['calls']
                if call[0] == name and call[-2:] != ['--jq', '.permissions.push // false']]

    def assert_canary(self, canary, others):
        self.assertNotIn('[skip ci]', self.message(canary))
        self.assertTrue(self.message(canary).startswith('Bootstrap common workflows: '))
        for name in others:
            self.assertTrue(self.message(name).endswith(SKIP_CI), self.message(name))
            self.assertEqual(self.message(name).count('[skip ci]'), 1)

    def test_canary_is_pushed_without_skip_ci_and_every_later_commit_with_it(self):
        self.initialize(['Alpha', 'Fundamentals', 'Beta'], runs={'Fundamentals': [
            [run('queued')],
            [run('completed', 'success'), run('completed', 'skipped', 'Docs'), run('completed', 'neutral', 'Lint')]]})
        out = self.run_bootstrap(['Alpha', 'Fundamentals', 'Beta'])
        # Green from the second read on, but a verdict needs SETTLE elapsed and two equal consecutive reads.
        self.assertEqual(self.state['events'], ['push Fundamentals'] + [f'sleep {POLL}'] * (SETTLE // POLL)
                         + [f'sleep {STAGGER}', 'push Alpha', f'sleep {STAGGER}', 'push Beta'])
        self.assert_canary('Fundamentals', ['Alpha', 'Beta'])
        self.assertEqual(self.state['run_reads'], {'Fundamentals': SETTLE // POLL + 1})
        self.assertIn('Canary Cratis/Fundamentals: 3 workflow run(s) succeeded', out)
        self.assertIn('Processed 3 repo(s), pushed 3 commit(s)', out)

    def test_canary_is_the_first_preferred_repository_that_needs_a_change(self):
        self.initialize(['Alpha', 'Fundamentals', 'Beta'])
        self.run_bootstrap(['Alpha', 'Fundamentals', 'Beta'], CANARY_PREFERENCE='Missing, Beta Fundamentals')
        self.assertEqual([e for e in self.state['events'] if e.startswith('push')],
                         ['push Beta', 'push Fundamentals', 'push Alpha'])
        self.assert_canary('Beta', ['Fundamentals', 'Alpha'])
        self.assertEqual(self.state['run_reads'], {'Beta': SETTLE // POLL + 1})

    def test_canary_falls_back_to_the_first_repository_that_needs_a_change(self):
        self.initialize(['Alpha', 'Fundamentals', 'Beta'], up_to_date=['Fundamentals'])
        self.run_bootstrap(['Alpha', 'Fundamentals', 'Beta'])
        self.assertEqual([e for e in self.state['events'] if e.startswith('push')], ['push Alpha', 'push Beta'])
        self.assert_canary('Alpha', ['Beta'])
        self.assertEqual(self.message('Fundamentals').count('[skip ci]'), 0)
        self.assertEqual(list(self.state['run_reads']), ['Alpha'])

    def test_a_failed_canary_run_stops_the_rollout_before_any_other_repository(self):
        for conclusion in ['failure', 'cancelled', 'startup_failure', 'timed_out', 'action_required']:
            with self.subTest(conclusion=conclusion):
                self.initialize(['Alpha', 'Fundamentals'], runs={'Fundamentals': [
                    [run('completed', 'success', 'Docs'), run('in_progress')],
                    [run('completed', 'success', 'Docs'), run('completed', conclusion)]]})
                out = self.run_bootstrap(['Alpha', 'Fundamentals'], expected=1)
                self.assertEqual(self.touched('Alpha'), [])
                self.assertEqual(self.state['events'], ['push Fundamentals', f'sleep {POLL}'])
                self.assertIn('Build: completed ' + conclusion, out)
                self.assertIn('::error::Canary Cratis/Fundamentals did not pass', out)

    def test_a_run_created_after_an_all_green_read_still_fails_the_rollout(self):
        self.initialize(['Alpha', 'Fundamentals'], runs={'Fundamentals': [
            [run('completed', 'success', 'Docs')],
            [run('completed', 'success', 'Docs'), run('completed', 'failure', 'Late')]]})
        out = self.run_bootstrap(['Alpha', 'Fundamentals'], expected=1)
        self.assertIn('Late: completed failure', out)
        self.assertEqual(self.touched('Alpha'), [])
        self.assertEqual(self.state['events'], ['push Fundamentals', f'sleep {POLL}'])

    def test_success_waits_for_two_equal_reads_after_the_settle_time(self):
        green = [run('completed', 'success')]
        both = green + [run('completed', 'skipped', 'Docs')]
        # The read at the settle time sees a new run, so the verdict waits for one more, equal read.
        self.initialize(['Fundamentals'], runs={'Fundamentals': [green, green, green, both]})
        out = self.run_bootstrap(['Fundamentals'])
        self.assertEqual(self.state['run_reads'], {'Fundamentals': SETTLE // POLL + 2})
        self.assertIn('Canary Cratis/Fundamentals: 2 workflow run(s) succeeded', out)

    def test_a_token_that_cannot_read_actions_runs_stops_before_any_push(self):
        self.initialize(['Alpha', 'Fundamentals'], actions_unreadable=['Fundamentals'])
        out = self.run_bootstrap(['Alpha', 'Fundamentals'], expected=1)
        self.assertIn('cannot read Actions runs in canary Cratis/Fundamentals; it needs the Actions: Read permission', out)
        self.assertEqual(self.state['events'], [])
        self.assertFalse([call for call in self.state['calls'] if '-X' in call and call[call.index('-X') + 1] != 'POST'])
        self.assertFalse([call for call in self.state['calls'] if any(arg.endswith(('/git/trees', '/git/commits'))
                                                                     for arg in call)])
        self.assertEqual(self.touched('Alpha'), [])

    def test_a_canary_that_does_not_complete_in_time_fails_the_rollout(self):
        self.initialize(['Alpha', 'Fundamentals'], runs={'Fundamentals': [[run('in_progress')]]})
        out = self.run_bootstrap(['Alpha', 'Fundamentals'], expected=1)
        self.assertIn(f'not complete after {TIMEOUT}s', out)
        self.assertEqual(self.touched('Alpha'), [])
        self.assertEqual(self.state['events'], ['push Fundamentals'] + [f'sleep {POLL}'] * (TIMEOUT // POLL))

    def test_unreadable_canary_runs_fail_the_rollout(self):
        self.initialize(['Alpha', 'Fundamentals'], runs={'Fundamentals': ['error']})
        out = self.run_bootstrap(['Alpha', 'Fundamentals'], expected=1)
        self.assertIn('Could not read the workflow runs of canary Cratis/Fundamentals', out)
        self.assertEqual(self.state['run_reads'], {'Fundamentals': 3})
        self.assertEqual(self.touched('Alpha'), [])

    def test_a_canary_that_starts_no_runs_proceeds_after_the_settle_time(self):
        self.initialize(['Alpha', 'Fundamentals'])
        out = self.run_bootstrap(['Alpha', 'Fundamentals'])
        self.assertIn(f'Canary Cratis/Fundamentals started no workflow runs within {SETTLE}s; proceeding', out)
        self.assertEqual(self.state['events'], ['push Fundamentals'] + [f'sleep {POLL}'] * (SETTLE // POLL)
                         + [f'sleep {STAGGER}', 'push Alpha'])
        self.assert_canary('Fundamentals', ['Alpha'])

    def test_stagger_waits_once_after_each_push_before_reading_the_next_head(self):
        names = ['Fundamentals', 'Alpha', 'Gamma', 'Beta', 'Delta']
        self.initialize(names, runs={'Fundamentals': [[run('completed', 'success')]]}, up_to_date=['Alpha', 'Gamma'])
        out = self.run_bootstrap(names)
        # Alpha takes the wait after the canary, so Gamma (also up to date) and Beta do not wait again.
        self.assertEqual(self.state['events'], ['push Fundamentals'] + [f'sleep {POLL}'] * (SETTLE // POLL)
                         + [f'sleep {STAGGER}', 'push Beta', f'sleep {STAGGER}', 'push Delta'])
        for name in ['Alpha', 'Delta']:
            self.assertIn(f'Processing Cratis/{name}...\n  Waiting {STAGGER}s after the previous push (stagger)', out)
        self.assert_canary('Fundamentals', ['Beta', 'Delta'])
        self.assertIn('Processed 5 repo(s), pushed 3 commit(s)', out)

    def test_an_up_to_date_run_pushes_nothing_and_waits_for_no_canary(self):
        self.initialize(['Fundamentals', 'Alpha'], up_to_date=['Fundamentals', 'Alpha'])
        out = self.run_bootstrap(['Fundamentals', 'Alpha'])
        self.assertEqual(self.state['events'], [])
        self.assertFalse([call for call in self.state['calls'] if any('/actions/runs' in arg for arg in call)])
        self.assertIn('Processed 2 repo(s), pushed 0 commit(s)', out)

    def test_an_invalid_timing_setting_cannot_run(self):
        self.initialize(['Fundamentals'])
        out = self.run_bootstrap(['Fundamentals'], expected=2, STAGGER_SECONDS='soon')
        self.assertIn("STAGGER_SECONDS must be a whole number of seconds, got 'soon'", out)
        self.assertEqual(self.state['calls'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
