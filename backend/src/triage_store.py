"""Schema and storage for offline triage rule packages and review-point claims.

Each review point gets one claim row per package. The claim row is the
resumable progress record: checkpoints overwrite it (never duplicate it) and
the claim token makes retries idempotent without sending a new package.
"""

import json
from datetime import datetime
from threading import Lock
from uuid import uuid4

from .db import connection

_schema_lock = Lock()
_schema_ready = False

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS triage_rule_packages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        package_no VARCHAR(60) UNIQUE NOT NULL,
        tree_version VARCHAR(60) NOT NULL,
        content_hash CHAR(64) NOT NULL,
        package_json MEDIUMTEXT NOT NULL,
        status ENUM('issued') NOT NULL DEFAULT 'issued',
        issued_by INT NULL,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (issued_by) REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS triage_package_claims (
        id INT AUTO_INCREMENT PRIMARY KEY,
        package_id INT NOT NULL,
        review_point VARCHAR(120) NOT NULL,
        claim_token CHAR(36) NOT NULL,
        status ENUM('checking','submitted','accepted','returned') NOT NULL DEFAULT 'checking',
        total_leaves INT NOT NULL,
        checked_leaves INT NOT NULL DEFAULT 0,
        checkpoint_json MEDIUMTEXT NULL,
        submitted_at DATETIME NULL,
        returned_mismatches_json MEDIUMTEXT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_claim_token (claim_token),
        UNIQUE KEY uq_package_reviewpoint (package_id, review_point),
        FOREIGN KEY (package_id) REFERENCES triage_rule_packages(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS triage_package_events (
        id INT AUTO_INCREMENT PRIMARY KEY,
        package_id INT NOT NULL,
        claim_id INT NULL,
        action VARCHAR(40) NOT NULL,
        actor VARCHAR(160) NOT NULL,
        detail TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (package_id) REFERENCES triage_rule_packages(id) ON DELETE CASCADE,
        FOREIGN KEY (claim_id) REFERENCES triage_package_claims(id) ON DELETE SET NULL
    )
    """,
]


def ensure_schema():
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        with connection() as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA_STATEMENTS:
                    cur.execute(statement)
        _schema_ready = True


def query(sql, params=()):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def execute(sql, params=()):
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid


def create_package(package_no, tree_version, content_hash, package_json, issued_by):
    return execute(
        """INSERT INTO triage_rule_packages(package_no,tree_version,content_hash,package_json,issued_by)
           VALUES(%s,%s,%s,%s,%s)""",
        (package_no, tree_version, content_hash, json.dumps(package_json, ensure_ascii=False), issued_by),
    )


def get_package_row(package_id):
    rows = query("SELECT * FROM triage_rule_packages WHERE id=%s", (package_id,))
    return rows[0] if rows else None


def list_package_rows():
    return query(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM triage_package_claims c WHERE c.package_id=p.id) AS claim_count,
                  (SELECT COUNT(*) FROM triage_package_claims c WHERE c.package_id=p.id AND c.status='checking') AS open_claim_count
           FROM triage_rule_packages p ORDER BY p.id DESC"""
    )


def create_claim(package_id, review_point, total_leaves):
    return execute(
        """INSERT INTO triage_package_claims(package_id,review_point,claim_token,status,total_leaves)
           VALUES(%s,%s,%s,'checking',%s)""",
        (package_id, review_point.strip()[:120], uuid4().hex, total_leaves),
    )


def get_claim_by_id(claim_id):
    rows = query("SELECT * FROM triage_package_claims WHERE id=%s", (claim_id,))
    return rows[0] if rows else None


def get_claim_by_token(token):
    rows = query("SELECT * FROM triage_package_claims WHERE claim_token=%s", (token,))
    return rows[0] if rows else None


def get_claim_by_review_point(package_id, review_point):
    rows = query(
        "SELECT * FROM triage_package_claims WHERE package_id=%s AND review_point=%s",
        (package_id, review_point.strip()[:120]),
    )
    return rows[0] if rows else None


def list_claim_rows(package_id=None):
    if package_id is None:
        return query("""SELECT c.*,p.package_no,p.status package_status
                        FROM triage_package_claims c JOIN triage_rule_packages p ON p.id=c.package_id
                        ORDER BY c.id DESC""")
    return query("SELECT * FROM triage_package_claims WHERE package_id=%s ORDER BY id", (package_id,))


def save_checkpoint(claim_id, checked_leaves, checkpoint):
    execute(
        "UPDATE triage_package_claims SET checked_leaves=%s,checkpoint_json=%s WHERE id=%s",
        (checked_leaves, json.dumps(checkpoint, ensure_ascii=False), claim_id),
    )


def reopen_claim(claim_id):
    execute(
        """UPDATE triage_package_claims
           SET status='checking',returned_mismatches_json=NULL,submitted_at=NULL WHERE id=%s""",
        (claim_id,),
    )


def mark_submitted(claim_id, checkpoint):
    execute(
        """UPDATE triage_package_claims
           SET status='submitted',submitted_at=%s,checked_leaves=%s,checkpoint_json=%s,
               returned_mismatches_json=NULL WHERE id=%s""",
        (datetime.utcnow(), checkpoint["checked_leaves"],
         json.dumps(checkpoint, ensure_ascii=False), claim_id),
    )


def mark_claim_returned(claim_id, mismatches):
    execute(
        "UPDATE triage_package_claims SET status='returned',returned_mismatches_json=%s WHERE id=%s",
        (json.dumps(mismatches, ensure_ascii=False), claim_id),
    )


def mark_claim_accepted(claim_id):
    execute("UPDATE triage_package_claims SET status='accepted' WHERE id=%s", (claim_id,))


def add_event(package_id, claim_id, action, actor, detail):    execute(
        """INSERT INTO triage_package_events(package_id,claim_id,action,actor,detail)
           VALUES(%s,%s,%s,%s,%s)""",
        (package_id, claim_id, action, str(actor), json.dumps(detail, ensure_ascii=False)),
    )


def list_events(package_id):
    return query(
        """SELECT e.*,c.review_point FROM triage_package_events e
           LEFT JOIN triage_package_claims c ON c.id=e.claim_id
           WHERE e.package_id=%s ORDER BY e.id DESC""",
        (package_id,),
    )
