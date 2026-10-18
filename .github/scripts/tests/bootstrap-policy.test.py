# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline full bootstrap executions. The only gh executable is a local stateful fake."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / '.github/scripts/bootstrap-common-workflows.sh').read_text()
POLICY = '.github/workflows/verify-pull-request-policy.yml'
LEGACY = ['.github/workflows/' + name + '.yml' for name in
          ['verify-release-notes', 'verify-semver-label', 'verify-no-work-records']]
PAYLOADS = dict(re.findall(r'BOOTSTRAPPED_FILES\["([^"]+)"\]="([^"]+)"', SOURCE))


def blob(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


# State-file fake implements GitHub's blob/tree/commit/ref operations, without network.
FAKE = r'''#!/usr/bin/env python3
import base64, hashlib, json, os, sys
from pathlib import Path
p = Path(os.environ['STUB_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
s['calls'].append(a)
method = a[a.index('-X')+1] if '-X' in a else 'GET'
endpoint = next(x for x in a if x.startswith('repos/'))
fields = dict(a[i+1].split('=',1) for i,x in enumerate(a) if x in ['-f','-F'])
body = json.load(sys.stdin) if '--input' in a else {}
error = None
result = None
def oid(x):
    return hashlib.sha1(json.dumps(x,sort_keys=True).encode()).hexdigest()
def tree_id(entries):
    sha = oid(entries)
    s['trees'][sha] = entries
    return sha
if method == 'GET' and endpoint.count('/') == 2:
    result = {'default_branch':'main','permissions':{'push':True}}
elif '/git/ref/heads/' in endpoint:
    result = {'object':{'sha':s['head']}}
elif method == 'GET' and '/git/commits/' in endpoint:
    result = s['commits'][endpoint.split('/')[-1]]
elif method == 'GET' and '/git/trees/' in endpoint:
    sha = endpoint.split('/')[-1].split('?')[0]
    result = {'tree':s['trees'][sha], 'truncated':s.get('truncated',False)}
elif method == 'GET' and '/git/blobs/' in endpoint:
    sha = endpoint.split('/')[-1]
    if s.get('fail_discovery'): error = 'HTTP 503 discovery failed'
    else: result = {'content':s['blobs'][sha]}
elif method == 'POST' and endpoint.endswith('/git/blobs'):
    data = base64.b64decode(fields['content'])
    if s.get('fail_policy') and b'name: Verify Pull Request Policy' in data:
        error = 'HTTP 503 replacement failed'
    else:
        sha = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        s['blobs'][sha] = fields['content']
        result = {'sha':sha}
elif method == 'POST' and endpoint.endswith('/git/trees'):
    entries = {e['path']:e for e in s['trees'][body['base_tree']]}
    for e in body['tree']:
        if e['sha'] is None: entries.pop(e['path'],None)
        else: entries[e['path']] = e
    s['tree_writes'].append(body)
    result = {'sha':tree_id(list(entries.values()))}
elif method == 'POST' and endpoint.endswith('/git/commits'):
    sha = oid(body)
    s['commits'][sha] = {'tree':{'sha':body['tree']},'parents':body['parents']}
    result = {'sha':sha}
elif method == 'PATCH' and '/git/refs/heads/' in endpoint:
    s['patches'] += 1
    assert fields['force'] == 'false'
    if s.get('race') and s['patches'] == 1:
        # Simulate a concurrent customized wrapper: fresh-head retry must preserve it.
        old_tree = s['trees'][s['commits'][s['head']]['tree']['sha']]
        entries = [e for e in old_tree if e['path'] != '.github/workflows/verify-release-notes.yml']
        data = b'name: Custom concurrent notes\n'
        sha = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        s['blobs'][sha] = base64.b64encode(data).decode()
        entries.append({'path':'.github/workflows/verify-release-notes.yml','type':'blob','mode':'100644','sha':sha})
        fresh_tree = tree_id(entries)
        s['head'] = 'c'*40
        s['commits'][s['head']] = {'tree':{'sha':fresh_tree}}
        error = 'Update is not a fast forward (HTTP 422)'
    elif s.get('always_race'):
        error = 'Update is not a fast forward (HTTP 422)'
    elif s['commits'][fields['sha']]['parents'] != [s['head']]:
        error = 'Update is not a fast forward (HTTP 422)'
    else:
        s['head'] = fields['sha']
        result = {'object':{'sha':s['head']}}
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
    elif q == '.content': print(result['content'])
    else: raise Exception(q)
else:
    print(json.dumps(result))
'''


class BootstrapPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bootstrap-policy-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state_file = self.directory / 'state.json'
        gh = self.directory / 'gh'
        gh.write_text(FAKE)
        gh.chmod(0o755)
        (self.directory / 'repos.json').write_text('["Example"]')
        self.env = {**os.environ, 'PATH': str(self.directory) + os.pathsep + os.environ['PATH'],
                    'STUB_STATE': str(self.state_file), 'REPOS_FILE': str(self.directory / 'repos.json'),
                    'GITHUB_WORKSPACE': str(self.directory), 'REPOS_IGNORE': '[]', 'TMPDIR': str(self.directory)}

    def initialize(self, files=None, **options):
        files = files if files is not None else {path: base64.b64decode(PAYLOADS[path]).decode() for path in LEGACY}
        blobs = {}
        entries = []
        for path, content in files.items():
            sha = blob(content)
            blobs[sha] = base64.b64encode(content.encode()).decode()
            entries.append({'path': path, 'sha': sha, 'mode': '100644', 'type': 'blob'})
        state = {'blobs': blobs, 'head': 'a'*40, 'commits': {'a'*40: {'tree': {'sha': 'b'*40}}},
                 'trees': {'b'*40: entries}, 'calls': [], 'tree_writes': [], 'patches': 0, **options}
        self.state_file.write_text(json.dumps(state))

    def run_bootstrap(self, expected=0):
        result = subprocess.run(['bash', str(ROOT / '.github/scripts/bootstrap-common-workflows.sh')],
                                cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.state = json.loads(self.state_file.read_text())
        return result.stdout + result.stderr

    def files(self):
        tree = self.state['trees'][self.state['commits'][self.state['head']]['tree']['sha']]
        return {e['path']: base64.b64decode(self.state['blobs'][e['sha']]).decode() for e in tree}

    def test_atomic_replacement_deletion_and_idempotent_release_continuity(self):
        self.initialize()
        self.run_bootstrap()
        self.assertEqual(len(self.state['tree_writes']), 1)
        entries = self.state['tree_writes'][0]['tree']
        self.assertIn(POLICY, [e['path'] for e in entries if e['sha']])
        self.assertEqual(set(LEGACY), {e['path'] for e in entries if e['sha'] is None})
        wrapper = self.files()[POLICY]
        self.assertIn('release-intent: true', wrapper)
        self.assertIn('needs: dependabot-labels', wrapper)
        self.assertIn("user.login == 'dependabot[bot]' && github.event.pull_request.base.ref == 'main'", wrapper)
        self.assertIn('pull-requests: write', wrapper)
        self.run_bootstrap()
        self.assertEqual(len(self.state['tree_writes']), 1)
        self.assertEqual(self.files()[POLICY], wrapper)
        self.assertEqual(self.state['patches'], 1)

    def test_customized_legacy_wrapper_is_preserved_and_logged(self):
        files = {p: base64.b64decode(PAYLOADS[p]).decode() for p in LEGACY}
        files[LEGACY[0]] = 'name: My custom notes\n'
        self.initialize(files)
        out = self.run_bootstrap()
        self.assertEqual(self.files()[LEGACY[0]], files[LEGACY[0]])
        self.assertIn('Keeping customized ' + LEGACY[0], out)

    def test_failed_replacement_creates_no_tree_commit_or_ref_update(self):
        self.initialize(fail_policy=True)
        self.run_bootstrap(expected=1)
        self.assertEqual(self.state['tree_writes'], [])
        self.assertEqual(self.state['patches'], 0)
        self.assertEqual(self.state['head'], 'a'*40)

    def test_failed_discovery_or_truncated_tree_is_not_a_nonreleasing_repository(self):
        for option in ['fail_discovery', 'truncated']:
            with self.subTest(option=option):
                self.initialize({'.github/workflows/publish.yml': 'uses: Cratis/release-action@v1\n'}, **{option: True})
                self.run_bootstrap(expected=1)
                self.assertEqual(self.state['tree_writes'], [])
                self.assertEqual(self.state['patches'], 0)

    def test_nonreleasing_and_arc_kotlin_callers_do_not_require_intent(self):
        for repo, files in [('Example', {}), ('Arc.Kotlin', {LEGACY[1]: base64.b64decode(PAYLOADS[LEGACY[1]]).decode()})]:
            with self.subTest(repo=repo):
                (self.directory / 'repos.json').write_text(json.dumps([repo]))
                self.initialize(files)
                self.run_bootstrap()
                wrapper = self.files()[POLICY]
                self.assertIn('release-intent: false', wrapper)
                self.assertNotIn('dependabot-labels:', wrapper)
                self.assertNotIn('needs:', wrapper)
                self.assertNotIn('    if: ${{ !cancelled() }}', wrapper)
                if repo == 'Arc.Kotlin': self.assertIn(LEGACY[1], self.files())

    def test_non_fast_forward_retries_once_from_fresh_head_and_rechecks_customizations(self):
        self.initialize(race=True)
        out = self.run_bootstrap()
        self.assertIn('retrying once from fresh HEAD', out)
        self.assertEqual(self.state['patches'], 2)
        self.assertEqual(self.files()[LEGACY[0]], 'name: Custom concurrent notes\n')
        self.assertEqual(self.state['commits'][self.state['head']]['parents'], ['c'*40])
        self.initialize(always_race=True)
        self.run_bootstrap(expected=1)
        self.assertEqual(self.state['patches'], 2)

    def test_wrapper_decodes_to_the_exact_rendered_caller(self):
        self.initialize()
        self.run_bootstrap()
        expected = '''name: Verify Pull Request Policy

on:
  pull_request:
    types: [opened, edited, reopened, synchronize, labeled, unlabeled, ready_for_review]
  push:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: read

jobs:
  dependabot-labels:
    if: github.event.pull_request.user.login == 'dependabot[bot]' && github.event.pull_request.base.ref == 'main'
    uses: Cratis/Workflows/.github/workflows/normalize-dependabot-labels.yml@main
    permissions:
      pull-requests: write

  policy:
    needs: dependabot-labels
    if: ${{ !cancelled() }}
    uses: Cratis/Workflows/.github/workflows/verify-pull-request-policy.yml@main
    with:
      runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}
      release-intent: true
'''
        self.assertEqual(self.files()[POLICY], expected)
        self.initialize({})
        self.run_bootstrap()
        nonreleasing = expected.replace('''  dependabot-labels:
    if: github.event.pull_request.user.login == 'dependabot[bot]' && github.event.pull_request.base.ref == 'main'
    uses: Cratis/Workflows/.github/workflows/normalize-dependabot-labels.yml@main
    permissions:
      pull-requests: write

''', '').replace('''    needs: dependabot-labels
    if: ${{ !cancelled() }}
''', '').replace('release-intent: true', 'release-intent: false')
        self.assertEqual(self.files()[POLICY], nonreleasing)
        readme = (ROOT / 'README.md').read_text().split('## Verifying release intent and release notes', 1)[1]
        sample = re.search(r'```yaml\n(.*?)```', readme, re.S)[1].replace(' # A repository that does not release', '')
        self.assertTrue(nonreleasing.endswith(sample))
        if shutil.which('actionlint'):
            caller = self.directory / 'caller.yml'
            for content in [expected, nonreleasing]:
                caller.write_text(content)
                subprocess.run(['actionlint', str(caller)], check=True, capture_output=True)

    def test_known_hash_table_covers_every_historical_payload_per_path(self):
        history = subprocess.check_output(['git', 'log', '-p', '--all', '--', '.github/scripts/bootstrap-common-workflows.sh'], cwd=ROOT, text=True)
        for path in LEGACY:
            values = re.findall(r'^[ +\-]?BOOTSTRAPPED_FILES\["' + re.escape(path) + r'"\]="([^"]+)"', history, re.M)
            self.assertTrue(values, path)
            recorded = re.search(r'LEGACY_WRAPPER_HASHES\["' + re.escape(path) + r'"\]="([^"]+)"', SOURCE)[1].split()
            self.assertEqual(set(recorded), {blob(base64.b64decode(v)) for v in values})


if __name__ == '__main__':
    unittest.main(verbosity=2)
