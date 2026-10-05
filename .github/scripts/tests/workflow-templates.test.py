# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline guards keeping incomplete examples out of GitHub's workflow discovery."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = ".github/templates/publish.template.yml"


class WorkflowTemplatesTests(unittest.TestCase):
    def test_publish_example_is_available_outside_executable_workflows(self):
        template = ROOT / TEMPLATE_PATH
        self.assertTrue(template.is_file())
        self.assertIn("name: Publish", template.read_text())
        self.assertFalse((ROOT / ".github/workflows/publish.template.yml").exists())

    def test_workflow_directory_contains_no_template_yaml(self):
        templates = [path.name for path in (ROOT / ".github/workflows").iterdir()
                     if path.suffix in {".yml", ".yaml"} and ".template." in path.name]
        self.assertEqual(templates, [], "Incomplete templates must not be discovered as workflows")

    def test_documentation_points_to_the_copyable_example(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn(f"[publish.template.yml](/{TEMPLATE_PATH})", readme)
        self.assertIn("`.github/workflows/publish.yml`", readme)
        context = (ROOT / ".cratis/ai/rules/project/what-lives-here.md").read_text()
        self.assertIn(TEMPLATE_PATH, context)
        for source in [readme, context]:
            self.assertNotIn(".github/workflows/publish.template.yml", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
