#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Turns a `cratis affected select` report into the outputs the change-detection job publishes.

`run-tests` is true when any test project must run at all, so the test job is skipped when a change reaches none.
A report that cannot be read counts as "run the tests": a skipped job must never be the result of a failure.
"""
import json
import os
import sys


def outputs(report_path):
    try:
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
        projects = report["projects"]
        run_tests = any(project["selection"]["mode"] != "skip" for project in projects)
        return {
            "run-tests": str(run_tests).lower(),
            "run-everything": str(bool(report["runEverything"])).lower(),
            "test-projects": str(sum(1 for project in projects if project["selection"]["mode"] != "skip")),
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"::warning title=affected::could not read {report_path}: {error}; running the tests", file=sys.stderr)
        return {"run-tests": "true", "run-everything": "true", "test-projects": "unknown"}


def main(argv):
    if len(argv) != 2:
        print("usage: affected-select-outputs.py <selection.json>", file=sys.stderr)
        return 2
    values = outputs(argv[1])
    lines = [f"{key}={value}" for key, value in values.items()]
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
