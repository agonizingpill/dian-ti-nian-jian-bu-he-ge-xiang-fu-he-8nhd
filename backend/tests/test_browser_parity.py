"""Cross-check the browser-side tree engine against the Python reference.

The frontend re-implements the fixed tree walk in plain JavaScript. This test
runs that implementation under Node against the exact package the Python
engine emits and asserts every leaf, path and threshold decision matches.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import triage_tree

NODE = shutil.which("node")
FRONTEND_ENGINE = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "triage-workbench.js")

HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
// Extract the pure evaluation function without loading the DOM-dependent module.
const match = source.match(/function twEvaluate\(tree, features\) \{[\s\S]*?\n\}/);
if (!match) { console.error('twEvaluate not found'); process.exit(2); }
eval(match[0]);
const pack = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const cases = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const out = [];
for (const features of cases) out.push(twEvaluate(pack.tree, features));
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js is required for the browser engine cross-check")
class BrowserEngineParityTests(unittest.TestCase):
    def test_browser_walk_matches_python_on_all_witness_inputs(self):
        pack = triage_tree.build_package()
        vectors = [leaf["witness_input"] for leaf in pack["leaves"]]
        # Add boundary and a couple of out-of-distribution vectors.
        vectors.extend([
            {"critical_fail_count": 0, "average_score": 80.0,
             "failed_item_count": 2, "documentation_gap": 0},
            {"critical_fail_count": 3, "average_score": 50.0,
             "failed_item_count": 5, "documentation_gap": 2},
            {"critical_fail_count": 0, "average_score": 99.99,
             "failed_item_count": 1, "documentation_gap": 0},
        ])
        expected = [triage_tree.evaluate(pack["tree"], vector) for vector in vectors]
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            pack_path = os.path.join(tmp, "pack.json")
            cases_path = os.path.join(tmp, "cases.json")
            with open(harness, "w") as fh:
                fh.write(HARNESS)
            with open(pack_path, "w") as fh:
                json.dump(pack, fh)
            with open(cases_path, "w") as fh:
                json.dump(vectors, fh)
            result = subprocess.run(
                [NODE, harness, os.path.abspath(FRONTEND_ENGINE), pack_path, cases_path],
                capture_output=True, text=True, check=True)
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(expected))
        for vector, py_result, js_result in zip(vectors, expected, actual):
            self.assertEqual(js_result["leaf_id"], py_result["leaf_id"], vector)
            self.assertEqual(js_result["prediction"], py_result["prediction"], vector)
            self.assertEqual(
                [(p["node_id"], p["branch"], p["next_node"]) for p in js_result["path"]],
                [(p["node_id"], p["branch"], p["next_node"]) for p in py_result["path"]],
                vector)


if __name__ == "__main__":
    unittest.main()
