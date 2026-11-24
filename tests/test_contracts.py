"""Check the design assets without treating examples as business runs."""

import json
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {"experience-draft", "experience-revise", "plan-draft", "plan-revise"}


def read_json(relative_path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{relative_path}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)


def refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"]
        for value in node.values():
            yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = read_json("contracts/workflow.json")
        cls.model = read_json("contracts/model-io.json")
        cls.scenarios = read_json("evals/scenarios.json")
        cls.desk = read_json("evals/desk-check.json")

    def ids(self, items):
        ids = [item["id"] for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        return set(ids)

    def test_workflow_and_scenario_references(self):
        workflow = self.workflow
        self.assertEqual(workflow["contract_version"], "0.3")
        self.assertEqual(self.ids(workflow["profiles"]), PROFILES)
        self.assertEqual(set(self.model["$defs"]["profile"]["enum"]), PROFILES)
        roles = set(workflow["source_roles"])
        self.assertEqual(set(self.model["$defs"]["source"]["properties"]["roles"]["items"]["enum"]), roles)
        for profile in workflow["profiles"]:
            self.assertLessEqual(set(profile["required_roles"]), roles)

        actions = self.ids(workflow["actions"])
        self.assertEqual(len(actions), 14)
        writes = {item["id"]: item["writes"] for item in workflow["actions"]}
        self.assertTrue(writes["register_task"])
        self.assertFalse(writes["query_status"])
        states = set(workflow["task_states"])
        self.assertIn("received", states)
        self.assertIn("unreviewed", workflow["quality_states"])
        for transition in workflow["transitions"]:
            self.assertLessEqual({transition["from"], transition["to"]}, states)
        for field in ("terminal_failure_from", "cancel_from"):
            self.assertLessEqual(set(workflow["transition_rules"][field]), states)
        self.assertLessEqual(
            set(workflow["export_policy"]["draft_quality_states_require_allow_draft"]),
            set(workflow["quality_states"]),
        )
        for action in workflow["actions"]:
            self.assertLessEqual(
                set(action.get("target_states", [])),
                set(workflow["entities"]["Revision"]["revision_states"]),
            )
        invariants = self.ids(workflow["invariants"])
        self.assertEqual(self.scenarios["status"], "not_run")
        self.assertEqual(len(self.ids(self.scenarios["cases"])), 17)
        self.assertEqual({case["profile_id"] for case in self.scenarios["cases"]}, PROFILES)
        for case in self.scenarios["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(case["status"], "not_run")
                self.assertLessEqual(set(case["actions"]), actions)
                self.assertLessEqual(set(case["invariants"]), invariants)

    def test_model_schema_and_synthetic_examples(self):
        self.assertEqual(self.desk["evidence_type"], "manual_contract_example")
        self.assertEqual(len(self.ids(self.desk["cases"])), 2)
        Draft202012Validator.check_schema(self.model)
        contract = self.workflow["model_contract"]
        for ref in {*refs(self.model), contract["request"], contract["response"]}:
            with self.subTest(ref=ref):
                self.assertTrue(ref.startswith("#/"))
                node = self.model
                for part in ref[2:].split("/"):
                    node = node[part.replace("~1", "/").replace("~0", "~")]
        validators = {
            kind: Draft202012Validator({
                "$schema": self.model["$schema"], "$defs": self.model["$defs"], "$ref": contract[kind],
            })
            for kind in ("request", "response")
        }
        for case in self.desk["cases"]:
            with self.subTest(case=case["id"]):
                validators["request"].validate(case["request"])
                validators["response"].validate(case["candidate"])
                if "valid_model_alternative" in case:
                    validators["response"].validate(case["valid_model_alternative"])

    def test_local_references(self):
        def check(source, target):
            resolved = (source.parent / target).resolve()
            self.assertTrue(resolved.is_relative_to(ROOT) and resolved.exists(), f"{source}: {target}")

        workflow_path = ROOT / "contracts/workflow.json"
        for field in ("prompt", "schema"):
            check(workflow_path, self.workflow["model_contract"][field])
        for profile in self.workflow["profiles"]:
            for filename in profile["rule_files"]:
                check(workflow_path, filename)
        check(ROOT / "contracts/model-io.json", "../docs/开工规格.md")

        markdown = list(ROOT.glob("*.md"))
        for directory in ("docs", "prompts", "skills", "evidence"):
            markdown.extend((ROOT / directory).rglob("*.md"))
        for source in markdown:
            for link in re.findall(r"\[[^\]\n]*\]\((<[^>\n]+>|[^)\n]+)\)", source.read_text(encoding="utf-8")):
                target = urlsplit(link.strip("<>"))
                if not (target.scheme or target.netloc) and target.path:
                    check(source, unquote(target.path))
