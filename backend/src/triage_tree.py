"""Fixed, versioned triage rule tree for offline review-point rule packages.

The tree is an explicit extraction of the regulator triage labels that the
online ``DecisionTreeClassifier`` job is trained on. Unlike the fitted model,
this tree is hand-authored, versioned and serialised into every rule package,
so an offline review point can evaluate cases without a network and the server
can replay every submitted leaf against the exact same tree.

The module deliberately uses the Python standard library only so it can be
unit tested in restricted environments and reused by scripts and workers.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

FEATURE_NAMES = [
    "critical_fail_count",
    "average_score",
    "failed_item_count",
    "documentation_gap",
]
FEATURE_TYPES = {
    "critical_fail_count": "int",
    "average_score": "decimal:2",
    "failed_item_count": "int",
    "documentation_gap": "int",
}
TREE_VERSION = "triage-tree-2026-11"
MODEL_REFERENCE = "DecisionTreeClassifier(random_state=1066,max_depth=3)"
ADVICE_TEXT = {
    "ready": "建议放行：进入快速复核通道",
    "review": "建议人工复核：提交主管逐项判断",
    "hold": "建议暂扣：退回整改并预约复检",
}

# Explicit depth-3 binary tree. A sample takes the LEFT branch when
# feature <= threshold and the RIGHT branch when feature > threshold.
TREE: dict[str, Any] = {
    "version": TREE_VERSION,
    "model_reference": MODEL_REFERENCE,
    "root": "N0",
    "max_depth": 3,
    "nodes": {
        "N0": {"type": "split", "node_id": "N0", "feature": "critical_fail_count",
               "threshold": 0.5, "left": "N1", "right": "L3"},
        "N1": {"type": "split", "node_id": "N1", "feature": "failed_item_count",
               "threshold": 1.5, "left": "L0", "right": "N2"},
        "N2": {"type": "split", "node_id": "N2", "feature": "average_score",
               "threshold": 80.0, "left": "L2", "right": "L1"},
        "L0": {"type": "leaf", "node_id": "L0", "label": "ready"},
        "L1": {"type": "leaf", "node_id": "L1", "label": "review"},
        "L2": {"type": "leaf", "node_id": "L2", "label": "hold"},
        "L3": {"type": "leaf", "node_id": "L3", "label": "hold"},
    },
}


class TriageTreeError(ValueError):
    """Raised when a tree, feature vector or submission is structurally invalid."""


# --------------------------------------------------------------------------- #
# Tree structure
# --------------------------------------------------------------------------- #

def nodes(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return tree["nodes"]


def split_nodes(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in tree["nodes"].values() if n["type"] == "split"]


def leaf_nodes(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in tree["nodes"].values() if n["type"] == "leaf"]


def leaf_ids(tree: dict[str, Any]) -> list[str]:
    return sorted(n["node_id"] for n in leaf_nodes(tree))


def validate_tree(tree: dict[str, Any]) -> None:
    """Fail fast on cycles, dangling children, unreachable nodes or depth drift."""
    if not isinstance(tree, dict) or "nodes" not in tree or "root" not in tree:
        raise TriageTreeError("规则树结构不完整")
    all_nodes = tree["nodes"]
    if tree["root"] not in all_nodes:
        raise TriageTreeError("根节点不存在")
    for node_id, node in all_nodes.items():
        if node.get("node_id") != node_id:
            raise TriageTreeError(f"节点 {node_id} 的标识不一致")
        if node["type"] == "split":
            for side in ("left", "right"):
                child = node.get(side)
                if child not in all_nodes:
                    raise TriageTreeError(f"节点 {node_id} 的 {side} 子节点缺失")
            if node.get("feature") not in FEATURE_NAMES:
                raise TriageTreeError(f"节点 {node_id} 引用了未知特征")
            if not isinstance(node.get("threshold"), (int, float)):
                raise TriageTreeError(f"节点 {node_id} 分界值无效")
        elif node["type"] == "leaf":
            if node.get("label") not in ADVICE_TEXT:
                raise TriageTreeError(f"叶节点 {node_id} 的分流建议无效")
        else:
            raise TriageTreeError(f"节点 {node_id} 类型无效")

    reached: set[str] = set()
    stack = [(tree["root"], 0)]
    while stack:
        node_id, depth = stack.pop()
        if node_id in reached:
            raise TriageTreeError(f"节点 {node_id} 存在多条访问路径")
        reached.add(node_id)
        node = all_nodes[node_id]
        if node["type"] == "split":
            if depth >= tree.get("max_depth", 3):
                raise TriageTreeError(f"节点 {node_id} 超过固定树深度")
            stack.append((node["left"], depth + 1))
            stack.append((node["right"], depth + 1))
    missing = set(all_nodes) - reached
    if missing:
        raise TriageTreeError(f"存在不可达节点：{sorted(missing)}")
    leaves = leaf_nodes(tree)
    if not leaves:
        raise TriageTreeError("规则树没有可达叶")


# --------------------------------------------------------------------------- #
# Offline evaluation
# --------------------------------------------------------------------------- #

def _feature_value(features: dict[str, Any], feature: str) -> float:
    if not isinstance(features, dict) or feature not in features:
        raise TriageTreeError(f"输入缺少特征 {feature}")
    value = features[feature]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TriageTreeError(f"特征 {feature} 必须是数值")
    return float(value)


def evaluate(tree: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    """Walk the fixed tree offline and return the leaf, advice and full path."""
    validate_tree(tree)
    node_id = tree["root"]
    path: list[dict[str, Any]] = []
    guard = 0
    while nodes(tree)[node_id]["type"] == "split":
        guard += 1
        if guard > len(nodes(tree)):
            raise TriageTreeError("规则树遍历异常")
        node = nodes(tree)[node_id]
        value = _feature_value(features, node["feature"])
        branch = "left" if value <= float(node["threshold"]) else "right"
        next_node = node[branch]
        path.append({
            "node_id": node_id,
            "feature": node["feature"],
            "threshold": float(node["threshold"]),
            "value": value,
            "branch": branch,
            "next_node": next_node,
        })
        node_id = next_node
    leaf = nodes(tree)[node_id]
    return {
        "leaf_id": node_id,
        "prediction": leaf["label"],
        "advice": ADVICE_TEXT[leaf["label"]],
        "path": path,
    }


def witness_inputs(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build one feature vector that reaches every reachable leaf.

    Constraints collected along each root-to-leaf path are solved into the
    tightest integer / two-decimal values so boundary handling is exercised.
    """
    validate_tree(tree)
    witnesses: dict[str, dict[str, Any]] = {}
    for leaf in leaf_nodes(tree):
        target = leaf["node_id"]
        bounds: dict[str, dict[str, float | None]] = {
            name: {"min": None, "max": None} for name in FEATURE_NAMES
        }
        node_id = tree["root"]
        while nodes(tree)[node_id]["type"] == "split":
            node = nodes(tree)[node_id]
            side = "left" if node["left"] == _next_toward(tree, node_id, target) else "right"
            threshold = float(node["threshold"])
            bound = bounds[node["feature"]]
            if side == "left":  # value <= threshold
                bound["max"] = threshold if bound["max"] is None else min(bound["max"], threshold)
                node_id = node["left"]
            else:  # value > threshold
                bound["min"] = threshold if bound["min"] is None else max(bound["min"], threshold)
                node_id = node["right"]

        vector: dict[str, Any] = {}
        for name in FEATURE_NAMES:
            kind = FEATURE_TYPES[name]
            lower, upper = bounds[name]["min"], bounds[name]["max"]
            if kind == "int":
                if lower is not None:
                    value = int(lower) + 1  # smallest integer strictly greater
                elif upper is not None:
                    value = int(upper)  # .5 thresholds naturally floor here
                else:
                    value = 0
            else:  # decimal:2
                if lower is not None:
                    value = round(lower + 0.01, 2)
                elif upper is not None:
                    value = round(upper, 2)
                else:
                    value = 85.0
            if lower is not None and value <= lower:
                raise TriageTreeError(f"叶 {target} 的见证输入无法满足 {name}>{lower}")
            if upper is not None and value > upper:
                raise TriageTreeError(f"叶 {target} 的见证输入无法满足 {name}<={upper}")
            vector[name] = value
        # Defence in depth: the synthesised input must land on the target leaf.
        landed = evaluate(tree, vector)
        if landed["leaf_id"] != target:
            raise TriageTreeError(f"叶 {target} 的见证输入落到了 {landed['leaf_id']}")
        witnesses[target] = vector
    return witnesses


def _next_toward(tree: dict[str, Any], node_id: str, target: str) -> str:
    node = nodes(tree)[node_id]
    for child in (node["left"], node["right"]):
        seen: set[str] = set()
        stack = [child]
        while stack:
            current = stack.pop()
            if current == target:
                return child
            if current in seen:
                continue
            seen.add(current)
            child_node = nodes(tree)[current]
            if child_node["type"] == "split":
                stack.extend((child_node["left"], child_node["right"]))
    raise TriageTreeError(f"节点 {node_id} 无法到达叶 {target}")


# --------------------------------------------------------------------------- #
# Package build / hashing
# --------------------------------------------------------------------------- #

def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def package_hash(package: dict[str, Any]) -> str:
    """Digest of every package field except the digest itself."""
    return content_hash({k: v for k, v in package.items() if k != "content_hash"})


def build_package(issued_by: str | None = None, now: datetime | None = None,
                  package_no: str | None = None) -> dict[str, Any]:
    """Build a self-contained, hash-pinned offline rule package."""
    tree = deepcopy(TREE)
    validate_tree(tree)
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    witnesses = witness_inputs(tree)
    leaves = []
    for leaf in leaf_nodes(tree):
        leaf_id = leaf["node_id"]
        sample = evaluate(tree, witnesses[leaf_id])
        leaves.append({
            "leaf_id": leaf_id,
            "prediction": leaf["label"],
            "advice": ADVICE_TEXT[leaf["label"]],
            "witness_input": witnesses[leaf_id],
            "expected_path": sample["path"],
        })
    splits = [{
        "node_id": n["node_id"],
        "feature": n["feature"],
        "threshold": float(n["threshold"]),
        "left_child": n["left"],
        "right_child": n["right"],
    } for n in split_nodes(tree)]

    pack: dict[str, Any] = {
        "schema_version": 1,
        "tree_version": tree["version"],
        "model_reference": tree["model_reference"],
        "max_depth": tree["max_depth"],
        "generated_at": generated_at,
        "issued_by": issued_by,
        "feature_schema": {name: FEATURE_TYPES[name] for name in FEATURE_NAMES},
        "advice_text": ADVICE_TEXT,
        "tree": tree,
        "splits": splits,
        "leaves": leaves,
    }
    base_digest = content_hash(pack)
    number = package_no or f"TRP-{generated_at[:7].replace('-', '')}-{base_digest[:8].upper()}"
    pack["package_no"] = number
    # Final digest pins every artefact field (including the package number);
    # the digest field itself is excluded from the hashed payload.
    pack["content_hash"] = package_hash(pack)
    return pack


# --------------------------------------------------------------------------- #
# Submission verification
# --------------------------------------------------------------------------- #

def _mismatch(kind: str, target: str, message: str,
              input_vector: Any = None, expected: Any = None, actual: Any = None) -> dict[str, Any]:
    return {
        "type": kind,
        "target_ref": target,
        "message": message,
        "input": input_vector,
        "expected": expected,
        "actual": actual,
    }


def verify_submission(tree: dict[str, Any], submission: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay a checkpoint submission against the fixed tree.

    Every discrepancy is returned with the offending input so a returned
    package can be investigated leaf by leaf on the workbench.
    """
    validate_tree(tree)
    mismatches: list[dict[str, Any]] = []
    expected_leaves = set(leaf_ids(tree))
    leaf_results = submission.get("leaf_results")
    if not isinstance(leaf_results, list):
        raise TriageTreeError("送审内容缺少 leaf_results 清单")

    seen: set[str] = set()
    for result in leaf_results:
        if not isinstance(result, dict):
            mismatches.append(_mismatch("BAD_RECORD", "-", "叶核对记录格式不正确", actual=result))
            continue
        leaf_id = result.get("leaf_id")
        input_vector = result.get("input")
        if leaf_id not in expected_leaves:
            mismatches.append(_mismatch(
                "UNKNOWN_LEAF", str(leaf_id), "送审了固定树之外的叶节点",
                input_vector, sorted(expected_leaves), leaf_id))
            continue
        if leaf_id in seen:
            mismatches.append(_mismatch("DUPLICATE_LEAF", leaf_id, "同一可达叶被重复送审", input_vector))
            continue
        seen.add(leaf_id)
        try:
            engine_result = evaluate(tree, input_vector)
        except TriageTreeError as exc:
            mismatches.append(_mismatch("BAD_INPUT", leaf_id, str(exc), input_vector))
            continue

        if engine_result["leaf_id"] != leaf_id:
            mismatches.append(_mismatch(
                "LEAF_NOT_REACHED", leaf_id,
                f"输入实际命中 {engine_result['leaf_id']}，与所报叶 {leaf_id} 不一致",
                input_vector, leaf_id, engine_result["leaf_id"]))
        if result.get("prediction") != engine_result["prediction"]:
            mismatches.append(_mismatch(
                "ADVICE_MISMATCH", leaf_id, "分流建议与固定树计算结果不一致",
                input_vector, engine_result["prediction"], result.get("prediction")))
        if result.get("advice") != engine_result["advice"]:
            mismatches.append(_mismatch(
                "ADVICE_TEXT_MISMATCH", leaf_id, "建议文案与固定树定义不一致",
                input_vector, engine_result["advice"], result.get("advice")))

        reported_path = result.get("path")
        expected_path = engine_result["path"]
        if not isinstance(reported_path, list) or len(reported_path) != len(expected_path):
            mismatches.append(_mismatch(
                "PATH_MISMATCH", leaf_id, "命中分支路径长度与固定树不一致",
                input_vector, [p["node_id"] for p in expected_path],
                [p.get("node_id") for p in reported_path] if isinstance(reported_path, list) else reported_path))
        else:
            for index, (reported, expected) in enumerate(zip(reported_path, expected_path)):
                ref = f"{leaf_id}#step{index + 1}"
                if not isinstance(reported, dict):
                    mismatches.append(_mismatch("PATH_MISMATCH", ref, "分支记录格式不正确", input_vector, expected, reported))
                    continue
                for field, kind in (("node_id", "PATH_MISMATCH"), ("branch", "BRANCH_MISMATCH"),
                                    ("next_node", "BRANCH_MISMATCH"), ("threshold", "THRESHOLD_MISMATCH"),
                                    ("feature", "PATH_MISMATCH")):
                    if reported.get(field) != expected[field]:
                        mismatches.append(_mismatch(
                            kind, ref, f"路径上的 {field} 与固定树不一致",
                            input_vector, expected[field], reported.get(field)))

    for missing in sorted(expected_leaves - seen):
        mismatches.append(_mismatch("MISSING_LEAF", missing, "可达叶漏算，未随包送审"))

    reported_nodes = submission.get("node_checks")
    if not isinstance(reported_nodes, list):
        mismatches.append(_mismatch("BAD_RECORD", "-", "分界值核对清单 node_checks 格式不正确"))
        reported_nodes = []
    node_map = {n.get("node_id"): n for n in reported_nodes if isinstance(n, dict)}
    split_field = {"feature": "feature", "threshold": "threshold",
                   "left_child": "left", "right_child": "right"}
    for split in split_nodes(tree):
        node_id = split["node_id"]
        reported = node_map.get(node_id)
        if reported is None:
            mismatches.append(_mismatch("MISSING_NODE_CHECK", node_id, "分界节点未随包送审"))
            continue
        for field, kind in (("feature", "NODE_MISMATCH"), ("threshold", "THRESHOLD_MISMATCH"),
                            ("left_child", "BRANCH_MISMATCH"), ("right_child", "BRANCH_MISMATCH")):
            expected_value = split[split_field[field]]
            if reported.get(field) != expected_value:
                mismatches.append(_mismatch(
                    kind, node_id, f"分界节点 {field} 与固定树不一致",
                    expected=expected_value, actual=reported.get(field)))
    for node_id, reported in node_map.items():
        if node_id not in nodes(tree) or nodes(tree)[node_id]["type"] != "split":
            mismatches.append(_mismatch("UNKNOWN_NODE", str(node_id), "送审了固定树之外的分界节点",
                                        actual=node_id))
    return mismatches
