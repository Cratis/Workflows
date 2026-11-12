# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline tests for the dispatch-only, single-file Dependabot rollout."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location('rollout', Path(__file__).with_name('bootstrap-rollout.test.py'))
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)
SCRIPT = rollout.ROOT / '.github/scripts/bootstrap-dependabot.sh'
HELPER = rollout.ROOT / '.github/scripts/ensure-dependabot-config.py'
WORKFLOW = rollout.ROOT / '.github/workflows/bootstrap-dependabot.yml'
rollout.SCRIPT = SCRIPT
CANONICAL = '''version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: monthly
    open-pull-requests-limit: 1
    groups:
      actions:
        patterns:
          - "*"
'''
NUGET = '''# Stage.Axon dependencies: keep these comments and settings.
version: 2
updates:
  - package-ecosystem: "nuget"
    directory: "/"
    schedule:
      interval: weekly
    groups:
      dependencies:
        patterns: ["*"]
# trailing comment
'''


def merge(text, *options):
    return subprocess.run(['python3', *options, str(HELPER)], input=text, text=True,
                          capture_output=True, timeout=10)


class MergeTests(unittest.TestCase):
    def test_missing_is_exactly_canonical_even_without_optional_yaml(self):
        for options in [(), ('-S',)]:
            result = merge('', *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, CANONICAL)

    def test_append_preserves_original_bytes_and_is_idempotent(self):
        for original in [NUGET, NUGET.rstrip('\n'),
                         NUGET.replace('\n  ', '\n    ')]:
            with self.subTest(original=original):
                result = merge(original)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith(original))
                self.assertEqual(result.stdout.count('package-ecosystem: "github-actions"'), 1)
                self.assertTrue(result.stdout.endswith('\n'))
                self.assertEqual(merge(result.stdout).returncode, 10)
        result = merge(NUGET, '-S')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(NUGET))

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML validation')
    def test_available_yaml_parser_rejects_invalid_nested_yaml(self):
        result = merge(NUGET.replace('interval: weekly', 'interval: ['))
        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_existing_actions_in_either_quoting_or_two_entries_is_untouched(self):
        for ecosystem in ['github-actions', '"github-actions"', "'github-actions'"]:
            original = NUGET + '\n  - package-ecosystem: ' + ecosystem + '\n    directory: "/"\n'
            result = merge(original)
            self.assertEqual(result.returncode, 10, result.stderr)
            self.assertEqual(result.stdout, '')

    def test_unsafe_yaml_never_emits_a_replacement(self):
        for original in ['version: 2\nupdates: []\n', 'version: 2\n',
                         NUGET + 'enable-beta-ecosystems: true\n', NUGET.replace('\n', '\r\n'),
                         NUGET.replace('  - ', '\t- '), NUGET.replace('updates:', 'updates: &entries'),
                         NUGET + '  - <<: *other\n', NUGET + ' - package-ecosystem: npm\n',
                         'version: 2\nupdates:\n  # no entries\n']:
            with self.subTest(original=original):
                result = merge(original)
                self.assertEqual(result.returncode, 11, result.stderr)
                self.assertEqual(result.stdout, '')


class BootstrapDependabotTests(unittest.TestCase):
    initialize = rollout.BootstrapRolloutTests.initialize
    run_bootstrap = rollout.BootstrapRolloutTests.run_bootstrap
    message = rollout.BootstrapRolloutTests.message

    def setUp(self):
        rollout.BootstrapRolloutTests.setUp(self)
        self.env['DRY_RUN'] = 'false'
        # Use production scope defaults unless a test deliberately overrides them.
        for key in ['REPOS_IGNORE', 'DEPENDABOT_INCLUDE', 'DEPENDABOT_IGNORE', 'DRY_RUN_FILES']:
            self.env.pop(key, None)

    def seed(self, repo, text, path='.github/dependabot.yml'):
        state = json.loads(self.state_file.read_text())
        target = state['repos'][repo]
        data = text.encode()
        sha = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        target['blobs'][sha] = base64.b64encode(data).decode()
        target['trees']['b' * 40].append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': sha})
        self.state_file.write_text(json.dumps(state))

    def files(self, repo):
        target = self.state['repos'][repo]
        tree = target['commits'][target['head']]['tree']['sha']
        return {entry['path']: base64.b64decode(target['blobs'][entry['sha']]).decode()
                for entry in target['trees'][tree]}

    def test_create_append_existing_and_second_run(self):
        names = ['Fundamentals', 'Stage.Axon', 'Prompter', 'TwoEntries']
        self.initialize(names)
        self.seed('Stage.Axon', NUGET)
        self.seed('Prompter', CANONICAL)
        self.seed('TwoEntries', NUGET + CANONICAL.split('updates:\n')[1])
        self.run_bootstrap(names)
        self.assertEqual(self.files('Fundamentals'), {'.github/dependabot.yml': CANONICAL})
        self.assertEqual(self.message('Fundamentals'), 'Bootstrap Dependabot: add dependabot.yml')
        self.assertTrue(self.files('Stage.Axon')['.github/dependabot.yml'].startswith(NUGET))
        self.assertEqual(self.message('Stage.Axon'), 'Bootstrap Dependabot: update dependabot.yml')
        self.assertEqual(self.files('Prompter')['.github/dependabot.yml'], CANONICAL)
        for name in ['Prompter', 'TwoEntries']:
            self.assertEqual(self.message(name), 'Initial')
        before = {name: self.state['repos'][name]['head'] for name in names}
        self.run_bootstrap(names)
        self.assertEqual(before, {name: self.state['repos'][name]['head'] for name in names})
        for name in names:
            self.assertNotIn(rollout.SKIP_CI.strip(), self.message(name))
        self.assertFalse(any('/actions/' in ' '.join(call) or '/labels' in ' '.join(call)
                             for call in self.state['calls']))

    def test_variant_and_unmergeable_are_skipped_without_any_writes(self):
        names = ['Variant', 'Unsafe']
        self.initialize(names)
        self.seed('Variant', CANONICAL, '.github/dependabot.yaml')
        self.seed('Unsafe', 'version: 2\nupdates: []\n')
        output = self.run_bootstrap(names)
        self.assertNotIn('.github/dependabot.yml', self.files('Variant'))
        self.assertEqual(self.files('Unsafe')['.github/dependabot.yml'], 'version: 2\nupdates: []\n')
        self.assertIn('Cannot merge dependabot.yml in Unsafe safely, skipping', output)
        self.assertIn('Unsafe', output.split('DEPENDABOT-SKIPPED')[-1])
        self.assertFalse(any('-X' in call for call in self.state['calls']))

    def test_scope_includes_exceptions_without_touching_other_files(self):
        included = ['Identity', 'Studio', 'Stage', 'Arc.TypeScript', 'Dockerfiles', 'AI', 'Infrastructure', 'Ensemble']
        excluded = ['Arc.Kotlin', 'Fundamentals.Kotlin', 'Arc.Elixir', 'acme-commerce-demo', 'SEO',
                    'Chronicle-ghsa-8c5g-wcfc-wgfh', 'Chronicle.Go', 'Arc.Go', 'Fundamentals.Go',
                    'Workflows', 'Chronicle.Python', 'Direct', 'Templates']
        names = included + excluded
        self.initialize(names)
        self.seed('Identity', NUGET)
        self.seed('Stage', 'name: Custom workflow\n', '.github/workflows/update-packages.yml')
        self.run_bootstrap(names)
        for name in included:
            self.assertIn('.github/dependabot.yml', self.files(name))
        self.assertEqual(self.files('Stage')['.github/workflows/update-packages.yml'], 'name: Custom workflow\n')
        for name in excluded:
            self.assertEqual(self.files(name), {})
            self.assertFalse(any(call[0] == name for call in self.state['calls']))

    def test_dry_run_is_read_only_and_reports_exact_actions_to_stdout_and_summary(self):
        names = ['Create', 'Update', 'Exists', 'Unsafe', 'Variant', 'Workflows']
        self.initialize(names)
        self.seed('Update', NUGET)
        self.seed('Exists', CANONICAL)
        self.seed('Unsafe', 'version: 2\nupdates: []\n')
        self.seed('Variant', CANONICAL, '.github/dependabot.yaml')
        summary = self.directory / 'summary.md'
        output = self.run_bootstrap(names, DRY_RUN='true', GITHUB_STEP_SUMMARY=str(summary))
        expected = ['DRYRUN Create add .github/dependabot.yml',
                    'DRYRUN Update update .github/dependabot.yml',
                    'DRYRUN Exists skip-exists .github/dependabot.yml',
                    'DRYRUN Unsafe skip-unmergeable .github/dependabot.yml',
                    'DRYRUN Variant skip-exists .github/dependabot.yaml']
        self.assertEqual([line for line in output.splitlines() if line.startswith('DRYRUN ')], expected)
        self.assertEqual([line for line in summary.read_text().splitlines() if line.startswith('DRYRUN ')], expected)
        self.assertIn('DRYRUN-SUMMARY', output)
        self.assertFalse(any('-X' in call for call in self.state['calls']), self.state['calls'])
        self.assertEqual(self.state['events'], [])
        self.assertTrue(all(repo['head'] == 'a' * 40 for repo in self.state['repos'].values()))

    def test_script_defaults_to_dry_run_and_rejects_invalid_dry_run(self):
        self.initialize(['Create'])
        self.env.pop('DRY_RUN')
        output = self.run_bootstrap(['Create'])
        self.assertIn('DRYRUN Create add .github/dependabot.yml', output)
        self.assertFalse(any('-X' in call for call in self.state['calls']))
        self.run_bootstrap(['Create'], expected=2, DRY_RUN='typo')

    def test_no_eligible_repositories_is_a_successful_read_only_noop(self):
        self.initialize(['Workflows'])
        output = self.run_bootstrap(['Workflows'], DRY_RUN='true')
        self.assertIn('Summary: 0 commit(s), 0 failure(s)', output)
        self.assertEqual(self.state['calls'], [])
        self.run_bootstrap([], DRY_RUN='true')
        self.assertEqual(self.state['calls'], [])

    def test_invalid_repository_file_fails_before_api_calls(self):
        self.initialize(['Create'])
        for invalid in [{'name': 'Create'}, [42]]:
            self.run_bootstrap(invalid, expected=1)
            self.assertEqual(self.state['calls'], [])

    def test_branch_movement_retries_once_using_fresh_head(self):
        self.initialize(['Create'])
        fake = self.directory / 'gh'
        fake.write_text(rollout.FAKE.replace(
            "    if r['commits'][fields['sha']]['parents'] != [r['head']]:",
            "    if not s.get('moved'):\n"
            "        s['moved'] = True\n"
            "        r['commits']['c'*40] = r['commits'][r['head']]\n"
            "        r['head'] = 'c'*40\n"
            "    if r['commits'][fields['sha']]['parents'] != [r['head']]:"))
        output = self.run_bootstrap(['Create'])
        self.assertIn('Branch moved; retrying once from fresh HEAD', output)
        self.assertEqual(self.files('Create'), {'.github/dependabot.yml': CANONICAL})
        self.assertEqual(len([call for call in self.state['calls'] if 'PATCH' in call]), 2)

    def test_failed_blob_creates_no_tree_commit_or_ref_and_fails_run(self):
        self.initialize(['Create'])
        fake = self.directory / 'gh'
        fake.write_text(rollout.FAKE.replace("    result = {'sha':sha}\nelif method == 'POST' and endpoint.endswith('/git/trees'):",
                                            "    error = 'HTTP 403 blob rejected'\nelif method == 'POST' and endpoint.endswith('/git/trees'):"))
        self.run_bootstrap(['Create'], expected=1)
        writes = [call for call in self.state['calls'] if '-X' in call]
        self.assertEqual(len(writes), 1)
        self.assertTrue(any(endpoint.endswith('/git/blobs') for endpoint in writes[0]))


class WorkflowTests(unittest.TestCase):
    def test_rollout_is_dispatch_only_and_dry_by_default(self):
        source = WORKFLOW.read_text()
        events = re.search(r'^on:\n(.*?)(?=^[^ #\n])', source, re.M | re.S)[1]
        self.assertEqual(re.findall(r'^  ([a-z_]+):', events, re.M), ['workflow_dispatch'])
        self.assertIn('        default: true', events)
        self.assertIn('        type: boolean', events)
        self.assertIn('permissions:\n  contents: read', source)
        self.assertIn('timeout-minutes:', source)
        self.assertIn('cancel-in-progress: false', source)
        self.assertIn('GH_TOKEN: ${{ secrets.PAT_WORKFLOWS }}', source)
        self.assertIn('gh repo list Cratis --limit 1000 --json name,isArchived', source)
        self.assertIn('select(.isArchived == false)', source)
        self.assertIn('DRY_RUN: ${{ inputs.dry_run }}', source)
        self.assertIn('bash .github/scripts/bootstrap-dependabot.sh', source)
        self.assertRegex(source, r'uses: actions/checkout@[0-9a-f]{40} # v7\.0\.1')
        self.assertIn('persist-credentials: false', source)

    def test_verification_paths_only_cover_the_standalone_rollout(self):
        source = WORKFLOW.with_name('verify-bootstrap-dependabot.yml').read_text()
        self.assertNotIn('bootstrap-common-workflows', source)
        for name in ['bootstrap-dependabot.sh', 'ensure-dependabot-config.py', 'bootstrap-dependabot.test.py',
                     'bootstrap-dependabot.yml', 'verify-bootstrap-dependabot.yml']:
            self.assertIn(name, source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
