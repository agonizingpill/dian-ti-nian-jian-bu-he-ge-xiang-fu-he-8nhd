"""Service workflow tests with an in-memory store (standard library only).

These cover the workflow rules that do not need a live MySQL/FastAPI stack:
idempotent claims, resumable checkpoints, the review gate (incomplete
packages cannot be submitted), automatic return on mismatch, supervisor
return/resume, and package-to-package isolation.
"""

import copy
import os
import sys
import types
import unittest
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Lightweight shims so the workflow service can be exercised without the web
# stack or a MySQL driver installed in the local test environment.
if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi
if "pymysql" not in sys.modules:
    pymysql = types.ModuleType("pymysql")
    pymysql.cursors = types.SimpleNamespace(DictCursor=object)
    pymysql.connect = lambda *a, **k: None
    sys.modules["pymysql"] = pymysql
    sys.modules["pymysql.cursors"] = pymysql.cursors

from src import triage_service as service
from src import triage_tree as tree

HTTPException = sys.modules["fastapi"].HTTPException

OPERATOR = {"sub": "1", "name": "李倩", "email": "operator@jingcheng.com", "role": "operator"}
SUPERVISOR = {"sub": "2", "name": "周岚", "email": "supervisor@jingcheng.com", "role": "supervisor"}
SUPERVISOR = {"sub": "2", "name": "周岚", "email": "supervisor@jingcheng.com", "role": "supervisor"}


class MemoryStore:
    def __init__(self):
        self.packages = {}
        self.claims = {}
        self.events = []
        self._package_seq = 0
        self._claim_seq = 0

    # package helpers ------------------------------------------------------ #
    def create_package(self, package_no, tree_version, content_hash, package_json, issued_by):
        self._package_seq += 1
        import json
        self.packages[self._package_seq] = {
            "id": self._package_seq, "package_no": package_no, "tree_version": tree_version,
            "content_hash": content_hash, "package_json": json.dumps(package_json, ensure_ascii=False),
            "status": "issued", "issued_by": issued_by, "issued_at": None,
        }
        return self._package_seq

    def get_package_row(self, package_id):
        return copy.deepcopy(self.packages.get(package_id))

    def list_package_rows(self):
        rows = []
        for row in self.packages.values():
            enriched = copy.deepcopy(row)
            claims = [c for c in self.claims.values() if c["package_id"] == row["id"]]
            enriched["claim_count"] = len(claims)
            enriched["open_claim_count"] = sum(1 for c in claims if c["status"] == "checking")
            rows.append(enriched)
        return sorted(rows, key=lambda r: r["id"], reverse=True)

    def create_claim(self, package_id, review_point, total_leaves):
        self._claim_seq += 1
        self.claims[self._claim_seq] = {
            "id": self._claim_seq, "package_id": package_id, "review_point": review_point,
            "claim_token": uuid4().hex, "status": "checking", "total_leaves": total_leaves,
            "checked_leaves": 0, "checkpoint_json": None, "submitted_at": None,
            "returned_mismatches_json": None, "created_at": None, "updated_at": None,
        }
        return self._claim_seq

    def get_claim_by_id(self, claim_id):
        return copy.deepcopy(self.claims.get(claim_id))

    def get_claim_by_token(self, token):
        for claim in self.claims.values():
            if claim["claim_token"] == token:
                return copy.deepcopy(claim)
        return None

    def get_claim_by_review_point(self, package_id, review_point):
        for claim in self.claims.values():
            if claim["package_id"] == package_id and claim["review_point"] == review_point:
                return copy.deepcopy(claim)
        return None

    def list_claim_rows(self, package_id=None):
        rows = []
        for claim in self.claims.values():
            if package_id is not None and claim["package_id"] != package_id:
                continue
            row = copy.deepcopy(claim)
            row["package_no"] = self.packages[claim["package_id"]]["package_no"]
            row["package_status"] = self.packages[claim["package_id"]]["status"]
            rows.append(row)
        return sorted(rows, key=lambda r: r["id"], reverse=True)

    def save_checkpoint(self, claim_id, checked_leaves, checkpoint):
        import json
        self.claims[claim_id].update(checked_leaves=checked_leaves,
                                     checkpoint_json=json.dumps(checkpoint, ensure_ascii=False))

    def reopen_claim(self, claim_id):
        self.claims[claim_id].update(status="checking", returned_mismatches_json=None,
                                     submitted_at=None)

    def mark_submitted(self, claim_id, checkpoint):
        from datetime import datetime
        import json
        self.claims[claim_id].update(
            status="submitted", submitted_at=datetime.utcnow(),
            checked_leaves=checkpoint["checked_leaves"],
            checkpoint_json=json.dumps(checkpoint, ensure_ascii=False),
            returned_mismatches_json=None)

    def mark_claim_returned(self, claim_id, mismatches):
        import json
        self.claims[claim_id].update(
            status="returned",
            returned_mismatches_json=json.dumps(mismatches, ensure_ascii=False))

    def mark_claim_accepted(self, claim_id):
        self.claims[claim_id]["status"] = "accepted"

    def add_event(self, package_id, claim_id, action, actor, detail):
        self.events.append({"package_id": package_id, "claim_id": claim_id,
                            "action": action, "actor": actor, "detail": detail})

    def list_events(self, package_id):
        return [dict(e) for e in self.events if e["package_id"] == package_id]


class TriageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryStore()
        self._original_store = service.store
        service.store = self.memory
        self.package_view = service.issue_package(SUPERVISOR)
        self.package_id = self.package_view["id"]
        self.pack = service.download_package(self.package_id)
        self.total = len(tree.leaf_ids(self.pack["tree"]))

    def tearDown(self):
        service.store = self._original_store

    def _honest_submission(self):
        leaf_results = []
        for leaf in self.pack["leaves"]:
            result = tree.evaluate(self.pack["tree"], leaf["witness_input"])
            leaf_results.append({"leaf_id": leaf["leaf_id"], "input": leaf["witness_input"],
                                 "prediction": result["prediction"], "advice": result["advice"],
                                 "path": result["path"]})
        node_checks = [{
            "node_id": split["node_id"], "feature": split["feature"],
            "threshold": split["threshold"], "left_child": split["left"],
            "right_child": split["right"],
        } for split in tree.split_nodes(self.pack["tree"])]
        return leaf_results, node_checks

    def test_repeated_claim_returns_same_token_and_never_duplicates(self):
        first, first_created = service.claim_package(self.package_id, "江宁分站 A", OPERATOR)
        second, second_created = service.claim_package(self.package_id, "江宁分站 A", OPERATOR)
        self.assertEqual(first["claim_token"], second["claim_token"])
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(len([c for c in self.memory.claims.values()
                              if c["review_point"] == "江宁分站 A"]), 1)

    def test_two_review_points_keep_independent_progress(self):
        point_a, _ = service.claim_package(self.package_id, "复核点 A", OPERATOR)
        point_b, _ = service.claim_package(self.package_id, "复核点 B", OPERATOR)
        leaf_results, node_checks = self._honest_submission()
        service.save_progress(point_a["claim_token"],
                              {"leaf_results": leaf_results[:1], "node_checks": node_checks}, OPERATOR)
        self.assertEqual(service.get_claim_view(point_a["claim_token"])["checked_leaves"], 1)
        self.assertEqual(service.get_claim_view(point_b["claim_token"])["checked_leaves"], 0)

    def test_incomplete_claim_cannot_be_submitted(self):
        claim, _ = service.claim_package(self.package_id, "复核点 C", OPERATOR)
        leaf_results, node_checks = self._honest_submission()
        with self.assertRaises(HTTPException) as ctx:
            service.submit_for_review(claim["claim_token"], {
                "package_hash": self.package_view["content_hash"],
                "leaf_results": leaf_results[:-1], "node_checks": node_checks}, OPERATOR)
        self.assertEqual(ctx.exception.detail["code"], "CHECK_INCOMPLETE")
        self.assertEqual(self.memory.get_claim_by_token(claim["claim_token"])["status"], "checking")

    def test_mismatched_submission_is_returned_with_inputs(self):
        claim, _ = service.claim_package(self.package_id, "复核点 D", OPERATOR)
        leaf_results, node_checks = self._honest_submission()
        leaf_results[0]["prediction"] = "hold"  # tampered advice
        with self.assertRaises(HTTPException) as ctx:
            service.submit_for_review(claim["claim_token"], {
                "package_hash": self.package_view["content_hash"],
                "leaf_results": leaf_results, "node_checks": node_checks}, OPERATOR)
        self.assertEqual(ctx.exception.detail["code"], "REVIEW_RETURNED")
        self.assertTrue(ctx.exception.detail["mismatches"])
        self.assertIn("input", ctx.exception.detail["mismatches"][0])
        stored = self.memory.get_claim_by_token(claim["claim_token"])
        self.assertEqual(stored["status"], "returned")

    def test_honest_submission_then_supervisor_accept(self):
        claim, _ = service.claim_package(self.package_id, "复核点 E", OPERATOR)
        leaf_results, node_checks = self._honest_submission()
        view, created = service.submit_for_review(claim["claim_token"], {
            "package_hash": self.package_view["content_hash"],
            "leaf_results": leaf_results, "node_checks": node_checks}, OPERATOR)
        self.assertTrue(created)
        self.assertEqual(view["status"], "submitted")
        # Idempotent retry does not create another event.
        events_before = len(self.memory.events)
        retry_view, retry_created = service.submit_for_review(claim["claim_token"], {
            "package_hash": self.package_view["content_hash"],
            "leaf_results": leaf_results, "node_checks": node_checks}, OPERATOR)
        self.assertFalse(retry_created)
        self.assertEqual(len(self.memory.events), events_before)
        accepted = service.accept_claim(view["id"], SUPERVISOR)
        self.assertEqual(accepted["status"], "accepted")

    def test_supervisor_return_can_be_resumed_and_resubmitted(self):
        claim, _ = service.claim_package(self.package_id, "复核点 F", OPERATOR)
        leaf_results, node_checks = self._honest_submission()
        view, _ = service.submit_for_review(claim["claim_token"], {
            "package_hash": self.package_view["content_hash"],
            "leaf_results": leaf_results, "node_checks": node_checks}, OPERATOR)
        service.return_claim(view["id"], "现场分界值记录与包不一致，需重核", SUPERVISOR)
        returned = self.memory.get_claim_by_token(claim["claim_token"])
        self.assertEqual(returned["status"], "returned")
        # Resume on the same claim row.
        resumed = service.save_progress(claim["claim_token"],
                                        {"leaf_results": leaf_results, "node_checks": node_checks}, OPERATOR)
        self.assertEqual(resumed["status"], "checking")
        again, created = service.submit_for_review(claim["claim_token"], {
            "package_hash": self.package_view["content_hash"],
            "leaf_results": leaf_results, "node_checks": node_checks}, OPERATOR)
        self.assertTrue(created)
        self.assertEqual(again["status"], "submitted")

    def test_returning_one_claim_does_not_affect_other_package(self):
        other = service.issue_package(SUPERVISOR)
        other_pack = service.download_package(other["id"])
        claim_a, _ = service.claim_package(self.package_id, "复核点 G", OPERATOR)
        claim_b, _ = service.claim_package(other["id"], "复核点 G", OPERATOR)
        self.assertNotEqual(claim_a["claim_token"], claim_b["claim_token"])
        leaf_a, node_a = self._honest_submission()
        leaf_b = []
        for leaf in other_pack["leaves"]:
            result = tree.evaluate(other_pack["tree"], leaf["witness_input"])
            leaf_b.append({"leaf_id": leaf["leaf_id"], "input": leaf["witness_input"],
                           "prediction": result["prediction"], "advice": result["advice"],
                           "path": result["path"]})
        node_b = [{"node_id": s["node_id"], "feature": s["feature"], "threshold": s["threshold"],
                   "left_child": s["left"], "right_child": s["right"]}
                  for s in tree.split_nodes(other_pack["tree"])]
        # Tamper package A so it gets returned.
        leaf_a[0]["prediction"] = "hold"
        with self.assertRaises(HTTPException):
            service.submit_for_review(claim_a["claim_token"], {
                "package_hash": self.package_view["content_hash"],
                "leaf_results": leaf_a, "node_checks": node_a}, OPERATOR)
        # Package B claim is untouched and can still submit successfully.
        view_b, created = service.submit_for_review(claim_b["claim_token"], {
            "package_hash": other["content_hash"],
            "leaf_results": leaf_b, "node_checks": node_b}, OPERATOR)
        self.assertTrue(created)
        self.assertEqual(view_b["status"], "submitted")

    def test_checkpoint_retry_overwrites_instead_of_duplicating(self):
        claim, _ = service.claim_package(self.package_id, "复核点 H", OPERATOR)
        leaf_results, node_checks = self._honest_submission()
        service.save_progress(claim["claim_token"],
                              {"leaf_results": leaf_results[:2], "node_checks": node_checks}, OPERATOR)
        service.save_progress(claim["claim_token"],
                              {"leaf_results": leaf_results[:2], "node_checks": node_checks}, OPERATOR)
        stored = self.memory.get_claim_by_token(claim["claim_token"])
        self.assertEqual(stored["checked_leaves"], 2)
        # One single claim row exists and it was overwritten rather than duplicated.
        matching = [c for c in self.memory.claims.values() if c["id"] == claim["id"]]
        self.assertEqual(len(matching), 1)
        # Checkpoints write no extra events: only issue + claim exist.
        self.assertEqual(len(self.memory.events), 2)
        self.assertEqual(
            [e["action"] for e in self.memory.events],
            ["issue_package", "claim_package"])


if __name__ == "__main__":
    unittest.main()
