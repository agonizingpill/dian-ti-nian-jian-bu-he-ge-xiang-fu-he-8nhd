"""Business rules for issuing, claiming, checking and returning rule packages."""

import json

from fastapi import HTTPException

from . import triage_store as store
from . import triage_tree as tree
from .actor_utils import actor_name


def _package_view(row):
    package = json.loads(row["package_json"])
    return {
        "id": row["id"],
        "package_no": row["package_no"],
        "tree_version": row["tree_version"],
        "content_hash": row["content_hash"],
        "status": row["status"],
        "issued_at": row["issued_at"],
        "claim_count": row.get("claim_count"),
        "open_claim_count": row.get("open_claim_count"),
        "leaf_count": len(tree.leaf_ids(package["tree"])),
        "tree_version_match": package["tree_version"] == row["tree_version"]
        and tree.package_hash(package) == row["content_hash"],
    }


def _claim_view(row):
    checkpoint = json.loads(row["checkpoint_json"]) if row.get("checkpoint_json") else None
    mismatches = json.loads(row["returned_mismatches_json"]) if row.get("returned_mismatches_json") else None
    return {
        "id": row["id"],
        "package_id": row["package_id"],
        "package_no": row.get("package_no"),
        "review_point": row["review_point"],
        "claim_token": row["claim_token"],
        "status": row["status"],
        "total_leaves": row["total_leaves"],
        "checked_leaves": row["checked_leaves"],
        "progress_pct": round(100 * row["checked_leaves"] / row["total_leaves"], 1),
        "checkpoint": checkpoint,
        "returned_mismatches": mismatches,
        "submitted_at": row.get("submitted_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "package_status": row.get("package_status"),
    }


def list_packages():
    return [_package_view(row) for row in store.list_package_rows()]


def list_claims():
    return [_claim_view(row) for row in store.list_claim_rows()]


def get_package(package_id):
    row = store.get_package_row(package_id)
    if not row:
        raise HTTPException(404, "分流规则包不存在")
    return row


def package_detail(package_id):
    row = get_package(package_id)
    view = _package_view(row)
    view["claims"] = [_claim_view(c) for c in store.list_claim_rows(package_id)]
    view["events"] = store.list_events(package_id)
    return view


def download_package(package_id):
    """The fixed package, exactly as issued, for offline workbench use."""
    row = get_package(package_id)
    package = json.loads(row["package_json"])
    if tree.package_hash(package) != row["content_hash"]:
        raise HTTPException(500, "规则包存储校验失败，禁止下载被篡改的包")
    return package


def issue_package(user):
    package = tree.build_package(issued_by=actor_name(user))
    package_id = store.create_package(
        package["package_no"], package["tree_version"],
        package["content_hash"], package, actor_name(user),
    )
    store.add_event(package_id, None, "issue_package", actor_name(user), {
        "package_no": package["package_no"],
        "tree_version": package["tree_version"],
        "content_hash": package["content_hash"],
        "leaf_count": len(package["leaves"]),
    })
    return _package_view(store.get_package_row(package_id))


def claim_package(package_id, review_point, user):
    """Hand out an offline package to a review point.

    The same review point retrying gets its original claim token back, so an
    interrupted claim never produces a duplicate hand-off.
    """
    row = get_package(package_id)
    point = (review_point or "").strip()
    if len(point) < 2:
        raise HTTPException(422, "请填写离线复核点名称")
    if row["status"] != "issued":
        raise HTTPException(409, "该规则包已退回，不能再向新复核点发放")
    existing = store.get_claim_by_review_point(package_id, point)
    if existing:
        store.add_event(package_id, existing["id"], "resume_claim", actor_name(user),
                        {"review_point": point, "checked_leaves": existing["checked_leaves"]})
        return _claim_view(existing), False
    package = json.loads(row["package_json"])
    total = len(tree.leaf_ids(package["tree"]))
    claim_id = store.create_claim(package_id, point, total)
    claim = store.get_claim_by_id(claim_id)
    store.add_event(package_id, claim_id, "claim_package", actor_name(user),
                    {"review_point": point, "total_leaves": total})
    return _claim_view(claim), True


def _require_claim(token):
    claim = store.get_claim_by_token(token)
    if not claim:
        raise HTTPException(404, "认领凭据无效或已失效，请重新认领规则包")
    return claim


def get_claim_view(token):
    return _claim_view(_require_claim(token))


def save_progress(token, checkpoint, user):
    """Persist one leaf's progress; retries overwrite instead of duplicating."""
    claim = _require_claim(token)
    if claim["status"] in ("submitted", "accepted"):
        raise HTTPException(409, "送审已完成，进度不再接受修改")
    leaf_results = checkpoint.get("leaf_results")
    node_checks = checkpoint.get("node_checks")
    if not isinstance(leaf_results, list) or not isinstance(node_checks, list):
        raise HTTPException(422, "进度快照必须包含 leaf_results 与 node_checks")
    leaf_ids = [item.get("leaf_id") for item in leaf_results if isinstance(item, dict)]
    if len(leaf_ids) != len(set(leaf_ids)):
        raise HTTPException(422, "进度快照存在重复叶记录")
    package = json.loads(store.get_package_row(claim["package_id"])["package_json"])
    valid = set(tree.leaf_ids(package["tree"]))
    unknown = [leaf_id for leaf_id in leaf_ids if leaf_id not in valid]
    if unknown:
        raise HTTPException(422, f"进度快照包含固定树之外的叶：{unknown}")
    payload = {
        "review_point": claim["review_point"],
        "package_hash": store.get_package_row(claim["package_id"])["content_hash"],
        "checked_leaves": len(leaf_ids),
        "total_leaves": claim["total_leaves"],
        "leaf_results": leaf_results,
        "node_checks": node_checks,
        "saved_at": checkpoint.get("saved_at"),
    }
    # A retry after a previous return resumes on the same claim row.
    if claim["status"] == "returned":
        store.reopen_claim(claim["id"])
    store.save_checkpoint(claim["id"], len(leaf_ids), payload)
    refreshed = store.get_claim_by_token(token)
    return _claim_view(refreshed)


def submit_for_review(token, submission, user):
    claim = _require_claim(token)
    if claim["status"] in ("submitted", "accepted"):
        # Idempotent: a retried submit returns the existing review state.
        return _claim_view(claim), False
    if claim["status"] != "checking":
        raise HTTPException(409, "当前认领状态不能送审")

    package_row = store.get_package_row(claim["package_id"])
    package = json.loads(package_row["package_json"])
    if submission.get("package_hash") != package_row["content_hash"]:
        raise HTTPException(409, "送审包与已发固定包的校验值不一致，请重新下载规则包")

    leaf_results = submission.get("leaf_results") or []
    if not isinstance(leaf_results, list) or len(leaf_results) < claim["total_leaves"]:
        raise HTTPException(409, {
            "code": "CHECK_INCOMPLETE",
            "message": f"还有可达叶未核对完（{len(leaf_results) if isinstance(leaf_results, list) else 0}/{claim['total_leaves']}），不能送审",
        })

    mismatches = tree.verify_submission(package["tree"], submission)
    if mismatches:
        store.mark_claim_returned(claim["id"], mismatches)
        store.add_event(claim["package_id"], claim["id"], "return_by_verification",
                        actor_name(user), {"mismatch_count": len(mismatches),
                                           "types": sorted({m["type"] for m in mismatches})})
        refreshed = store.get_claim_by_token(token)
        raise HTTPException(422, {"code": "REVIEW_RETURNED",
                                  "message": "核对发现漏算或不一致，已退回该复核点",
                                  "mismatches": mismatches,
                                  "claim": _claim_view(refreshed)})

    checkpoint = {
        "review_point": claim["review_point"],
        "package_hash": package_row["content_hash"],
        "checked_leaves": claim["total_leaves"],
        "total_leaves": claim["total_leaves"],
        "leaf_results": leaf_results,
        "node_checks": submission.get("node_checks") or [],
        "saved_at": submission.get("saved_at"),
    }
    store.mark_submitted(claim["id"], checkpoint)
    store.add_event(claim["package_id"], claim["id"], "submit_review",
                    actor_name(user), {"leaf_count": len(leaf_results)})
    refreshed = store.get_claim_by_token(token)
    return _claim_view(refreshed), True


def accept_claim(claim_id, user):
    rows = store.list_claim_rows()
    claim = next((c for c in rows if c["id"] == claim_id), None)
    if not claim:
        raise HTTPException(404, "认领记录不存在")
    if claim["status"] != "submitted":
        raise HTTPException(409, "只有待复核的认领记录可以通过")
    store.mark_claim_accepted(claim_id)
    store.add_event(claim["package_id"], claim_id, "accept_claim", actor_name(user),
                    {"review_point": claim["review_point"]})
    return _claim_view(store.get_claim_by_token(claim["claim_token"]))


def return_claim(claim_id, reason, user):
    rows = store.list_claim_rows()
    claim = next((c for c in rows if c["id"] == claim_id), None)
    if not claim:
        raise HTTPException(404, "认领记录不存在")
    note = (reason or "").strip()
    if len(note) < 4:
        raise HTTPException(422, "请写明退回原因（至少 4 个字符）")
    if claim["status"] != "submitted":
        raise HTTPException(409, "只有待复核的认领记录可以退回")
    mismatches = [{"type": "SUPERVISOR_RETURN", "target_ref": "-",
                   "message": note[:500], "input": None, "expected": None, "actual": None}]
    store.mark_claim_returned(claim_id, mismatches)
    store.add_event(claim["package_id"], claim_id, "return_by_supervisor",
                    actor_name(user), {"reason": note[:500]})
    return _claim_view(store.get_claim_by_token(claim["claim_token"]))
