import json
import os
import sys
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.db import connection, init_schema
from src.auth import hash_password
from src import triage_store
from src import triage_tree

USERS = [
    ("operator@jingcheng.com", "李倩", "operator"),
    ("supervisor@jingcheng.com", "周岚", "supervisor"),
    ("viewer@jingcheng.com", "顾言", "viewer"),
]
LIFTS = [
    ("NJ-YT-021", "云台花园 2 栋", "南京市秦淮区建邺路 88 号", "迅达", "S5500", 1000, 2019, date(2026,12,8)),
    ("NJ-XH-118", "新河商务中心", "南京市鼓楼区中山北路 169 号", "奥的斯", "Gen3", 800, 2018, date(2026,11,28)),
    ("NJ-JS-307", "金盛广场 A 座", "南京市建邺区江东中路 399 号", "日立", "HGP", 1350, 2021, date(2026,12,18)),
    ("NJ-LH-044", "龙湖天街 1 号梯", "南京市栖霞区学津路 9 号", "三菱", "NEXIEZ", 1000, 2020, date(2026,12,21)),
]
ITEMS = {
    "NJ-YT-021": [
        ("SAFETY_CIRCUIT", "安全回路联锁", "safety", True, 42, "fail", "门锁回路在 2 层短接测试未断开", "IMG-YT-021-01.jpg"),
        ("BRAKE_TEST", "制动器静态保持", "safety", True, 55, "fail", "制动距离 112mm，超过 100mm 限值", "IMG-YT-021-02.jpg"),
        ("DOOR_FORCE", "层门闭合力", "mechanical", False, 86, "pass", "闭合力 145N", "IMG-YT-021-03.jpg"),
        ("BUFFER_CLEAR", "缓冲器距离", "mechanical", False, 92, "pass", "底坑净距满足要求", "IMG-YT-021-04.jpg"),
        ("LOGBOOK", "维保记录完整性", "documentation", False, 88, "pass", "近三月记录齐全", "IMG-YT-021-05.jpg"),
    ],
    "NJ-XH-118": [
        ("SAFETY_CIRCUIT", "安全回路联锁", "safety", True, 94, "pass", "回路逐点测试通过", "IMG-XH-118-01.jpg"),
        ("BRAKE_TEST", "制动器静态保持", "safety", True, 93, "pass", "制动距离 76mm", "IMG-XH-118-02.jpg"),
        ("DOOR_FORCE", "层门闭合力", "mechanical", False, 90, "pass", "闭合力 132N", "IMG-XH-118-03.jpg"),
        ("BUFFER_CLEAR", "缓冲器距离", "mechanical", False, 87, "pass", "净距合格", "IMG-XH-118-04.jpg"),
        ("LOGBOOK", "维保记录完整性", "documentation", False, 96, "pass", "记录和签章齐全", "IMG-XH-118-05.jpg"),
    ],
    "NJ-JS-307": [
        ("SAFETY_CIRCUIT", "安全回路联锁", "safety", True, 91, "pass", "测试通过", "IMG-JS-307-01.jpg"),
        ("BRAKE_TEST", "制动器静态保持", "safety", True, 89, "pass", "制动距离 88mm", "IMG-JS-307-02.jpg"),
        ("DOOR_FORCE", "层门闭合力", "mechanical", False, 74, "fail", "西侧门闭合力 208N", "IMG-JS-307-03.jpg"),
        ("BUFFER_CLEAR", "缓冲器距离", "mechanical", False, 90, "pass", "净距合格", "IMG-JS-307-04.jpg"),
        ("LOGBOOK", "维保记录完整性", "documentation", False, 84, "pass", "记录齐全", "IMG-JS-307-05.jpg"),
    ],
    "NJ-LH-044": [
        ("SAFETY_CIRCUIT", "安全回路联锁", "safety", True, 95, "pass", "回路逐点测试通过", "IMG-LH-044-01.jpg"),
        ("BRAKE_TEST", "制动器静态保持", "safety", True, 94, "pass", "制动距离 79mm", "IMG-LH-044-02.jpg"),
        ("DOOR_FORCE", "层门闭合力", "mechanical", False, 89, "pass", "闭合力 149N", "IMG-LH-044-03.jpg"),
        ("BUFFER_CLEAR", "缓冲器距离", "mechanical", False, 93, "pass", "净距合格", "IMG-LH-044-04.jpg"),
        ("LOGBOOK", "维保记录完整性", "documentation", False, 90, "pass", "记录齐全", "IMG-LH-044-05.jpg"),
    ],
}

def seed():
    init_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            for email,name,role in USERS:
                cur.execute("INSERT INTO users(email,name,password_hash,role) VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name),role=VALUES(role),password_hash=VALUES(password_hash)", (email,name,hash_password("Demo@1066"),role))
            cur.execute("SELECT id,email FROM users")
            user_ids = {x["email"]: x["id"] for x in cur.fetchall()}
            for asset,building,address,mfr,model,load,year,due in LIFTS:
                cur.execute("INSERT INTO lifts(asset_code,building,address,manufacturer,model,rated_load,installed_year,inspection_due,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active') ON DUPLICATE KEY UPDATE building=VALUES(building),address=VALUES(address),inspection_due=VALUES(inspection_due)", (asset,building,address,mfr,model,load,year,due))
                cur.execute("SELECT id FROM lifts WHERE asset_code=%s", (asset,))
                lift_id = cur.fetchone()["id"]
                for code,title,category,critical,score,result,notes,photo in ITEMS[asset]:
                    cur.execute("INSERT INTO inspection_items(lift_id,code,title,category,critical,score,result,notes,photo_ref,inspected_at,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open') ON DUPLICATE KEY UPDATE score=VALUES(score),result=VALUES(result),notes=VALUES(notes),photo_ref=VALUES(photo_ref)", (lift_id,code,title,category,critical,score,result,notes,photo,datetime(2026,11,20,9,30)))
                cur.execute("SELECT id FROM inspection_items WHERE lift_id=%s AND code IN ('SAFETY_CIRCUIT','BRAKE_TEST') AND result='fail' LIMIT 1", (lift_id,))
                bad = cur.fetchone()
                if bad:
                    cur.execute("SELECT id FROM rectification_notices WHERE lift_id=%s AND inspection_item_id=%s", (lift_id,bad["id"]))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO rectification_notices(lift_id,inspection_item_id,issue_summary,severity,deadline,status,evidence_required,created_by) VALUES(%s,%s,%s,'critical',%s,'open','回路测试视频+接线端子照片',%s)", (lift_id,bad["id"],"关键安全项不合格，需整改后复检",date(2026,11,25),user_ids["operator@jingcheng.com"]))
            # A deliberately mismatched historical recheck shows the rejection branch in the UI without creating a valid duplicate.
            cur.execute("SELECT id FROM rectification_notices WHERE lift_id=(SELECT id FROM lifts WHERE asset_code='NJ-YT-021') LIMIT 1")
            notice = cur.fetchone()
            cur.execute("SELECT id FROM rechecks WHERE notice_id=%s AND status='evidence_rejected'", (notice["id"],))
            if notice and not cur.fetchone():
                cur.execute("INSERT INTO rechecks(notice_id,scheduled_at,inspector,status,photo_ref,photo_item_code,remarks,created_by) VALUES(%s,%s,'王工','evidence_rejected','IMG-YT-021-03.jpg','DOOR_FORCE','历史照片与安全回路项不匹配，已退回补证',%s)", (notice["id"],datetime(2026,11,23,14,0),user_ids["operator@jingcheng.com"]))
        seed_triage_package(cur, user_ids)
    print("seed_demo complete: users=3 lifts=4 inspection_items=20")


def seed_triage_package(cur, user_ids):
    """Issue one fixed offline rule package and one in-progress review-point claim."""
    triage_store.ensure_schema()
    cur.execute("SELECT id FROM triage_rule_packages WHERE package_no LIKE 'TRP-DEMO-%' LIMIT 1")
    if cur.fetchone():
        return
    package = triage_tree.build_package(issued_by="supervisor@jingcheng.com",
                                        package_no=f"TRP-DEMO-{datetime.utcnow().strftime('%Y%m')}-0001")
    cur.execute(
        """INSERT INTO triage_rule_packages(package_no,tree_version,content_hash,package_json,issued_by)
           VALUES(%s,%s,%s,%s,%s)""",
        (package["package_no"], package["tree_version"], package["content_hash"],
         json.dumps(package, ensure_ascii=False), user_ids["supervisor@jingcheng.com"]),
    )
    package_id = cur.lastrowid
    total_leaves = len(triage_tree.leaf_ids(package["tree"]))
    cur.execute(
        """INSERT INTO triage_package_claims(package_id,review_point,claim_token,status,total_leaves,checked_leaves)
           VALUES(%s,'江宁分站离线复核点 A',%s,'checking',%s,%s)""",
        (package_id, "demo-claim-offline-0001-0000-0000-000000000001", total_leaves, 1),
    )
    claim_id = cur.lastrowid
    first_leaf = package["leaves"][0]
    result = triage_tree.evaluate(package["tree"], first_leaf["witness_input"])
    checkpoint = {
        "review_point": "江宁分站离线复核点 A",
        "package_hash": package["content_hash"],
        "checked_leaves": 1,
        "total_leaves": total_leaves,
        "leaf_results": [{
            "leaf_id": first_leaf["leaf_id"],
            "input": first_leaf["witness_input"],
            "prediction": result["prediction"],
            "advice": result["advice"],
            "path": result["path"],
        }],
        "node_checks": [{
            "node_id": split["node_id"], "feature": split["feature"],
            "threshold": split["threshold"], "left_child": split["left_child"],
            "right_child": split["right_child"],
        } for split in package["splits"]],
        "saved_at": datetime.utcnow().isoformat(),
    }
    cur.execute("UPDATE triage_package_claims SET checkpoint_json=%s WHERE id=%s",
                (json.dumps(checkpoint, ensure_ascii=False), claim_id))
    cur.execute(
        """INSERT INTO triage_package_events(package_id,claim_id,action,actor,detail)
           VALUES(%s,%s,'claim_package','operator@jingcheng.com',%s)""",
        (package_id, claim_id, json.dumps({"review_point": "江宁分站离线复核点 A",
                                           "total_leaves": total_leaves}, ensure_ascii=False)),
    )

if __name__ == "__main__":
    seed()
