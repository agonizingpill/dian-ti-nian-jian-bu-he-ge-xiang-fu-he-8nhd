"""Unit tests for the offline triage rule tree engine.

Run with: python -m unittest discover -s backend/tests -v
The tests only rely on the Python standard library.
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import triage_tree as tt


class TreeStructureTests(unittest.TestCase):
    def test_every_declared_leaf_is_reachable(self):
        tree = copy.deepcopy(tt.TREE)
        tt.validate_tree(tree)
        reachable = set()
        stack = [tree["root"]]
        while stack:
            node_id = stack.pop()
            node = tree["nodes"][node_id]
            if node["type"] == "leaf":
                reachable.add(node_id)
            else:
                stack.extend((node["left"], node["right"]))
        self.assertEqual(reachable, set(tt.leaf_ids(tree)))
        self.assertEqual(len(tt.split_nodes(tree)), 3)
        self.assertEqual(len(tt.leaf_nodes(tree)), 4)

    def test_dangling_node_is_rejected(self):
        tree = copy.deepcopy(tt.TREE)
        tree["nodes"]["N0"]["left"] = "GHOST"
        with self.assertRaises(tt.TriageTreeError):
            tt.validate_tree(tree)

    def test_unreachable_node_is_rejected(self):
        tree = copy.deepcopy(tt.TREE)
        tree["nodes"]["N0"]["right"] = "L3"
        tree["nodes"]["EXTRA"] = {"type": "leaf", "node_id": "EXTRA", "label": "hold"}
        with self.assertRaises(tt.TriageTreeError):
            tt.validate_tree(tree)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tree = copy.deepcopy(tt.TREE)

    def test_ready_leaf_path(self):
        result = tt.evaluate(self.tree, {
            "critical_fail_count": 0, "average_score": 90.0,
            "failed_item_count": 0, "documentation_gap": 0})
        self.assertEqual(result["leaf_id"], "L0")
        self.assertEqual(result["prediction"], "ready")
        self.assertEqual([p["node_id"] for p in result["path"]], ["N0", "N1"])
        self.assertEqual([p["branch"] for p in result["path"]], ["left", "left"])

    def test_review_leaf_requires_three_decisions(self):
        result = tt.evaluate(self.tree, {
            "critical_fail_count": 0, "average_score": 88.0,
            "failed_item_count": 2, "documentation_gap": 0})
        self.assertEqual(result["leaf_id"], "L1")
        self.assertEqual(result["prediction"], "review")
        self.assertEqual(len(result["path"]), 3)
        self.assertEqual(result["path"][-1]["branch"], "right")

    def test_hold_leaf_on_low_score(self):
        result = tt.evaluate(self.tree, {
            "critical_fail_count": 0, "average_score": 66.0,
            "failed_item_count": 3, "documentation_gap": 1})
        self.assertEqual((result["leaf_id"], result["prediction"]), ("L2", "hold"))
        self.assertEqual(result["path"][-1]["branch"], "left")

    def test_any_critical_failure_holds_immediately(self):
        result = tt.evaluate(self.tree, {
            "critical_fail_count": 2, "average_score": 96.0,
            "failed_item_count": 2, "documentation_gap": 0})
        self.assertEqual((result["leaf_id"], result["prediction"]), ("L3", "hold"))
        self.assertEqual(len(result["path"]), 1)
        self.assertEqual(result["path"][0]["node_id"], "N0")

    def test_threshold_boundary_uses_left_on_equality(self):
        # average_score == 80.0 must take the left (hold) branch at N2.
        result = tt.evaluate(self.tree, {
            "critical_fail_count": 0, "average_score": 80.0,
            "failed_item_count": 2, "documentation_gap": 0})
        self.assertEqual(result["leaf_id"], "L2")
        self.assertEqual(result["path"][-1]["branch"], "left")

    def test_missing_feature_is_rejected(self):
        with self.assertRaises(tt.TriageTreeError):
            tt.evaluate(self.tree, {"critical_fail_count": 0})

    def test_non_numeric_feature_is_rejected(self):
        with self.assertRaises(tt.TriageTreeError):
            tt.evaluate(self.tree, {
                "critical_fail_count": "0", "average_score": 90.0,
                "failed_item_count": 0, "documentation_gap": 0})


class WitnessTests(unittest.TestCase):
    def test_every_leaf_has_a_witness_landing_on_it(self):
        tree = copy.deepcopy(tt.TREE)
        witnesses = tt.witness_inputs(tree)
        self.assertEqual(set(witnesses), set(tt.leaf_ids(tree)))
        for leaf_id, vector in witnesses.items():
            result = tt.evaluate(tree, vector)
            self.assertEqual(result["leaf_id"], leaf_id, vector)
            self.assertEqual(result["prediction"], tree["nodes"][leaf_id]["label"])

    def test_witnesses_cover_both_sides_of_each_split(self):
        tree = copy.deepcopy(tt.TREE)
        witnesses = tt.witness_inputs(tree)
        branches = {n["node_id"]: set() for n in tt.split_nodes(tree)}
        for vector in witnesses.values():
            result = tt.evaluate(tree, vector)
            for step in result["path"]:
                branches[step["node_id"]].add(step["branch"])
        for node_id, taken in branches.items():
            self.assertEqual(taken, {"left", "right"}, f"{node_id} 两侧分支未被覆盖")


class PackageTests(unittest.TestCase):
    def test_package_is_self_contained_and_hash_pinned(self):
        pack = tt.build_package(issued_by="tester")
        expected_keys = {
            "schema_version", "tree_version", "model_reference", "max_depth",
            "generated_at", "issued_by", "feature_schema", "advice_text",
            "tree", "splits", "leaves", "package_no", "content_hash",
        }
        self.assertTrue(expected_keys.issubset(pack))
        payload = {k: v for k, v in pack.items() if k != "content_hash"}
        self.assertEqual(pack["content_hash"], tt.content_hash(payload))
        self.assertEqual(len(pack["leaves"]), len(tt.leaf_nodes(pack["tree"])))
        # Each packaged leaf carries an input that lands on that leaf.
        for leaf in pack["leaves"]:
            self.assertEqual(
                tt.evaluate(pack["tree"], leaf["witness_input"])["leaf_id"],
                leaf["leaf_id"])

    def test_content_hash_changes_when_tree_changes(self):
        pack_a = tt.build_package()
        tampered = copy.deepcopy(pack_a)
        tampered["tree"]["nodes"]["N2"]["threshold"] = 75.0
        self.assertNotEqual(tt.content_hash(
            {k: v for k, v in tampered.items() if k != "content_hash"}),
            pack_a["content_hash"])

    def test_package_serialises_and_replays_identically(self):
        pack = tt.build_package()
        restored = json.loads(json.dumps(pack, ensure_ascii=False))
        self.assertEqual(restored["content_hash"], pack["content_hash"])
        for leaf in restored["leaves"]:
            result = tt.evaluate(restored["tree"], leaf["witness_input"])
            self.assertEqual(result["leaf_id"], leaf["leaf_id"])


class VerifySubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tree = copy.deepcopy(tt.TREE)
        self.pack = tt.build_package()

    def _honest_submission(self):
        leaf_results = []
        for leaf in self.pack["leaves"]:
            result = tt.evaluate(self.tree, leaf["witness_input"])
            leaf_results.append({
                "leaf_id": leaf["leaf_id"],
                "input": leaf["witness_input"],
                "prediction": result["prediction"],
                "advice": result["advice"],
                "path": result["path"],
            })
        node_checks = [{
            "node_id": n["node_id"], "feature": n["feature"],
            "threshold": n["threshold"], "left_child": n["left"],
            "right_child": n["right"],
        } for n in tt.split_nodes(self.tree)]
        return {"leaf_results": leaf_results, "node_checks": node_checks}

    def test_honest_submission_passes(self):
        self.assertEqual(tt.verify_submission(self.tree, self._honest_submission()), [])

    def test_missing_leaf_is_reported_with_its_id(self):
        submission = self._honest_submission()
        submission["leaf_results"].pop()
        problems = tt.verify_submission(self.tree, submission)
        self.assertEqual([p["type"] for p in problems], ["MISSING_LEAF"])
        self.assertIn(problems[0]["target_ref"], tt.leaf_ids(self.tree))

    def test_wrong_advice_is_reported_with_input(self):
        submission = self._honest_submission()
        submission["leaf_results"][0]["prediction"] = "hold"
        problems = tt.verify_submission(self.tree, submission)
        self.assertEqual([p["type"] for p in problems], ["ADVICE_MISMATCH"])
        self.assertEqual(problems[0]["expected"], "ready")
        self.assertEqual(problems[0]["actual"], "hold")
        self.assertIn("input", problems[0])

    def test_wrong_threshold_is_reported(self):
        submission = self._honest_submission()
        submission["node_checks"][2]["threshold"] = 75.0
        problems = tt.verify_submission(self.tree, submission)
        self.assertEqual([p["type"] for p in problems], ["THRESHOLD_MISMATCH"])
        self.assertEqual(problems[0]["expected"], 80.0)

    def test_input_that_hits_another_leaf_is_rejected(self):
        submission = self._honest_submission()
        target = next(r for r in submission["leaf_results"] if r["leaf_id"] == "L1")
        target["input"]["average_score"] = 60.0  # now lands on L2
        problems = tt.verify_submission(self.tree, submission)
        kinds = {p["type"] for p in problems}
        self.assertIn("LEAF_NOT_REACHED", kinds)
        self.assertTrue(any(p["actual"] == "L2" for p in problems))

    def test_duplicate_and_unknown_leaves_are_reported(self):
        submission = self._honest_submission()
        submission["leaf_results"].append(copy.deepcopy(submission["leaf_results"][0]))
        submission["leaf_results"].append({
            "leaf_id": "LX", "input": {}, "prediction": "hold",
            "advice": tt.ADVICE_TEXT["hold"], "path": []})
        kinds = {p["type"] for p in tt.verify_submission(self.tree, submission)}
        self.assertIn("DUPLICATE_LEAF", kinds)
        self.assertIn("UNKNOWN_LEAF", kinds)

    def test_path_branch_tampering_is_reported(self):
        submission = self._honest_submission()
        submission["leaf_results"][0]["path"][0]["branch"] = "right"
        kinds = {p["type"] for p in tt.verify_submission(self.tree, submission)}
        self.assertIn("BRANCH_MISMATCH", kinds)

    def test_missing_node_check_is_reported(self):
        submission = self._honest_submission()
        submission["node_checks"].pop()
        problems = tt.verify_submission(self.tree, submission)
        self.assertEqual([p["type"] for p in problems], ["MISSING_NODE_CHECK"])


if __name__ == "__main__":
    unittest.main()
