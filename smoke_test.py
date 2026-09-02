"""Isolated smoke + audit tests for the workforce dashboard.

Uses a temporary SQLite database (never the live instance/workforce.db),
so it can run any number of times without polluting production data.

Run:  python smoke_test.py
"""
import os
import sys
import tempfile

TEST_DB = os.path.join(tempfile.gettempdir(), "wf_smoke_test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB
os.environ.setdefault("SECRET_KEY", "test-secret")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as a

client = a.app.test_client()


def setup_module():
    with a.app.app_context():
        a.db.create_all()
        a._ensure_columns()
        a._seed()
        a._migrate_db()


# --------------------------- Auth ---------------------------
def test_login_page():
    assert client.get("/login").status_code == 200


def test_wrong_password_stays_on_login():
    # Legacy shared-password backdoor removed: wrong password stays on login.
    r = client.post("/login", data={"password": "definitely-wrong"})
    assert r.status_code == 200


def test_admin_login():
    r = client.post("/login",
                    data={"password": "admin123", "username": "admin",
                          "role": "admin"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"Workforce" in r.data


# --------------------------- Filters ---------------------------
def test_runs_invalid_date_400():
    r = client.get("/api/runs?date_from=not-a-date")
    assert r.status_code == 400, r.get_json()


def test_runs_machine_status_filter():
    with a.app.app_context():
        m = a.Machine.query.first()
    r = client.post("/api/run/start", json={"machine_id": m.id})
    assert r.status_code in (200, 201), r.get_json()
    r = client.get("/api/runs?machine_status=running")
    assert r.status_code == 200
    assert any(run["machine_id"] == m.id for run in r.get_json()["runs"])


def test_machines_accepts_date_filter():
    r = client.get("/api/machines?date=2026-01-01&shift=day")
    assert r.status_code == 200
    assert "machines" in r.get_json()


def test_reports_invalid_date_400():
    r = client.get("/api/reports?date_from=bad")
    assert r.status_code == 400, r.get_json()


def test_history_invalid_limit_400():
    r = client.get("/api/history?limit=abc")
    assert r.status_code == 400, r.get_json()


def test_machine_logs_invalid_limit_400():
    r = client.get("/api/machine-logs?limit=abc")
    assert r.status_code == 400, r.get_json()


# --------------------------- Entry persistence + validation ---------------------------
def test_record_upsert_persists():
    payload = {
        "record_date": "2026-08-13",
        "total_workforce": 120,
        "metex_staff": 20, "csk_staff": 25, "topquality_staff": 15,
        "bestcare_staff": 18, "prestige_staff": 22,
        "working_machines": 8, "out_of_order_machines": 2,
        "working_machine_names": "Excavator-1, Crane-2",
        "out_of_order_machine_names": "Drill-5",
    }
    r = client.post("/api/record", json=payload)
    assert r.status_code == 200, r.get_json()
    rec = r.get_json()["record"]
    assert rec["total_workforce"] == 120
    assert rec["working_machines"] == 8
    r = client.get("/api/record?date=2026-08-13")
    assert r.status_code == 200
    assert r.get_json()["record"]["prestige_staff"] == 22


def test_record_rejects_nonnumeric():
    before = client.get("/api/record?date=2026-08-14").get_json()
    r = client.post("/api/record", json={"record_date": "2026-08-14",
                                         "total_workforce": "12o"})
    assert r.status_code == 400, r.get_json()
    after = client.get("/api/record?date=2026-08-14").get_json()
    assert before.get("record") == after.get("record")


def test_run_start_validates_product_id():
    with a.app.app_context():
        m = a.Machine.query.first()
    r = client.post("/api/run/start", json={"machine_id": m.id,
                                            "product_id": 999999})
    assert r.status_code == 400, r.get_json()


def test_run_start_stop_roundtrip():
    with a.app.app_context():
        m = a.Machine.query.first()
    r = client.post("/api/run/start", json={"machine_id": m.id})
    assert r.status_code in (200, 201), r.get_json()
    rid = r.get_json()["run"]["id"]
    r = client.post(f"/api/run/stop/{rid}")
    assert r.status_code == 200, r.get_json()
    # Stopping a non-existent run must 404, not 500.
    assert client.post("/api/run/stop/999999").status_code == 404


def test_user_create_password_min_length():
    r = client.post("/api/users", json={"username": "newbie",
                                        "password": "ab", "role": "viewer"})
    assert r.status_code == 400, r.get_json()


# --------------------------- Access control ---------------------------
def _login_as(c, username, password, role):
    c.post("/login", data={"username": username, "password": password,
                          "role": role}, follow_redirects=False)


def test_operator_redirected_from_admin_pages():
    c = a.app.test_client()
    _login_as(c, "operator", "oper123", "operator")
    # HTML admin pages redirect (302) instead of a 403 error page.
    assert c.get("/summary").status_code == 302
    assert c.get("/users").status_code == 302
    # API admin endpoints return a clean JSON 403.
    r = c.get("/api/summary/times")
    assert r.status_code == 403, r.get_json()


def test_unauthenticated_redirects():
    c = a.app.test_client()
    assert c.get("/").status_code == 302


# --------------------------- Audit trail ---------------------------
def test_audit_page_requires_permission():
    c = a.app.test_client()
    _login_as(c, "operator", "oper123", "operator")
    # Operator is not granted the 'audit' page by default -> redirect.
    assert c.get("/audit").status_code == 302
    c2 = a.app.test_client()
    _login_as(c2, "admin", "admin123", "admin")
    # Admin can open the Audit Trail page.
    assert c2.get("/audit").status_code == 200


def test_audit_logs_record_login_and_user_create():
    c = a.app.test_client()
    _login_as(c, "admin", "admin123", "admin")
    # Creating a user should produce a user_create audit entry.
    r = c.post("/api/users", json={"username": "audited_user",
                                   "password": "pass1234", "role": "viewer"})
    assert r.status_code == 201, r.get_json()
    data = c.get("/api/audit-logs").get_json()
    actions = [l["action"] for l in data["logs"]]
    assert "login" in actions
    assert "user_create" in actions
    # Filtering by action returns only that action.
    f = c.get("/api/audit-logs?action=user_create").get_json()
    assert len(f["logs"]) >= 1
    assert all(l["action"] == "user_create" for l in f["logs"])


def test_audit_logs_filter_by_user():
    c = a.app.test_client()
    _login_as(c, "admin", "admin123", "admin")
    data = c.get("/api/audit-logs?user=admin").get_json()
    assert len(data["logs"]) >= 1
    # server-side filter is case-insensitive; assert the same way.
    assert all("admin" in (l["actor"] or "").lower() for l in data["logs"])


def test_audit_logs_export_returns_csv():
    c = a.app.test_client()
    _login_as(c, "admin", "admin123", "admin")
    r = c.get("/api/audit-logs/export")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/csv")
    assert "Time,User,Action" in r.data.decode()


if __name__ == "__main__":
    setup_module()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} TEST(S) FAILED")
        sys.exit(1)
    print("\nALL_TESTS_PASSED")

