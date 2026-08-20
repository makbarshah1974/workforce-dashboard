"""
Workforce & Machine Live Monitoring System
------------------------------------------
A password-protected, multi-user dashboard that tracks machines, products,
groups and workforce numbers per date, backed by PostgreSQL (or SQLite for
quick testing). Different users sign in with their own credentials and can
update machine status, which is reflected live in the dashboard and reports.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # then edit .env
    python app.py
"""
import csv
import io
import json
import os
from datetime import date, datetime, timezone, timedelta

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///workforce.db")
# SQLAlchemy needs postgresql:// (not postgres://) on newer versions
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
# Support psycopg (v3) driver explicitly if chosen in the URL
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")
PORT = int(os.environ.get("PORT", 5000))
# Debug mode. NEVER enable when exposed to the internet (Werkzeug debugger
# allows remote code execution). Defaults to off for safety.
DEBUG = os.environ.get("DEBUG", "False").strip().lower() in ("1", "true", "yes", "on")

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Timezone helper
# All activity timestamps (start/stop, status changes, logs) are recorded in
# UAE standard time (UTC+4) so they match the operators' local wall-clock and
# the front-end, which parses naive ISO strings as the viewer's local time.
# ---------------------------------------------------------------------------
UAE_TZ = timezone(timedelta(hours=4))


def now_uae():
    """Current UAE (UTC+4) wall-clock time as a naive datetime.

    Stored without tzinfo so the front-end's `new Date(iso)` interprets it as
    the viewer's local time — which is UAE time on the operators' machines.
    """
    return datetime.now(UAE_TZ).replace(tzinfo=None)


def shift_of(dt):
    """Return the shift ('day' | 'night') for a given datetime.

    Day shift: 07:00–19:00, Night shift: 19:00–07:00 (UAE local time).
    """
    if dt is None:
        return "day"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return "day"
    return "day" if 7 <= dt.hour < 19 else "night"


# ---------------------------------------------------------------------------
# Database models
# ---------------------------------------------------------------------------
class User(db.Model):
    """Application user. Each person logs in with their own credentials."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(120), default="")
    role = db.Column(db.String(32), default="operator")  # admin | supervisor | operator
    permissions = db.Column(db.Text, default="")  # JSON list of allowed page keys
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def permissions_list(self):
        if not self.permissions:
            return []
        try:
            val = json.loads(self.permissions)
            return val if isinstance(val, list) else []
        except (ValueError, TypeError):
            return []

    def to_dict(self, include_perms=True):
        d = {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "role": self.role,
            "is_active": self.is_active,
        }
        if include_perms:
            d["permissions"] = self.permissions_list
        return d


class Group(db.Model):
    """A department / production line that groups machines and products."""

    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, default="")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description}


class Product(db.Model):
    """A product produced by a group / machine."""

    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(64), default="")
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    target_qty = db.Column(db.Integer, default=0)
    unit = db.Column(db.String(32), default="units")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "group_id": self.group_id,
            "target_qty": self.target_qty,
            "unit": self.unit,
        }


class Machine(db.Model):
    """A single machine/equipment with a live status."""

    __tablename__ = "machines"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(64), default="")
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    status = db.Column(db.String(32), default="running")  # running|out_of_order|maintenance|idle
    location = db.Column(db.String(120), default="")
    notes = db.Column(db.Text, default="")
    updated_by = db.Column(db.String(120), default="")
    updated_at = db.Column(db.DateTime, default=now_uae, onupdate=now_uae)
    # Planned productive hours for this machine per shift (e.g. 9h vs 11h).
    day_hours = db.Column(db.Integer, default=11)
    night_hours = db.Column(db.Integer, default=11)

    def to_dict(self, group_name=None, product_name=None):
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "group_id": self.group_id,
            "group_name": group_name,
            "product_id": self.product_id,
            "product_name": product_name,
            "status": self.status,
            "location": self.location,
            "notes": self.notes,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "day_hours": self.day_hours,
            "night_hours": self.night_hours,
        }


class MachineLog(db.Model):
    """Audit trail of machine status changes (who changed what, when)."""

    __tablename__ = "machine_logs"

    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    status = db.Column(db.String(32), default="")
    note = db.Column(db.Text, default="")
    updated_by = db.Column(db.String(120), default="")
    timestamp = db.Column(db.DateTime, default=now_uae)
    shift = db.Column(db.String(16), default="day")  # "day" or "night"

    def to_dict(self, machine_name=None):
        return {
            "id": self.id,
            "machine_id": self.machine_id,
            "machine_name": machine_name,
            "status": self.status,
            "note": self.note,
            "updated_by": self.updated_by,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "shift": self.shift,
        }


class ProductionRun(db.Model):
    """A start/stop production session: a machine making a product item."""

    __tablename__ = "production_runs"

    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    item_name = db.Column(db.String(160), default="")   # product item name (plug & play)
    item_code = db.Column(db.String(80), default="")    # product item code
    operator = db.Column(db.String(120), default="")
    started_at = db.Column(db.DateTime, default=now_uae, nullable=False)
    stopped_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(32), default="running")  # running | stopped
    note = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    shift = db.Column(db.String(16), default="day")  # "day" or "night"

    def run_seconds(self):
        end = self.stopped_at or now_uae()
        if not self.started_at:
            return 0
        return int((end - self.started_at).total_seconds())

    def to_dict(self, machine_name=None, product_name=None, group_name=None,
                machine_status=None):
        secs = self.run_seconds()
        return {
            "id": self.id,
            "machine_id": self.machine_id,
            "machine_name": machine_name,
            "machine_status": machine_status,
            "product_id": self.product_id,
            "product_name": product_name,
            "group_id": self.group_id,
            "group_name": group_name,
            "item_name": self.item_name,
            "item_code": self.item_code,
            "operator": self.operator,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "status": self.status,
            "note": self.note,
            "shift": self.shift,
            "run_seconds": secs,
            "run_time": _fmt_duration(secs),
        }


class DailyRecord(db.Model):
    """One row per calendar date holding the workforce + machine snapshot."""

    __tablename__ = "daily_records"

    __table_args__ = (
        db.UniqueConstraint("record_date", "shift", name="uq_date_shift"),
    )

    id = db.Column(db.Integer, primary_key=True)
    record_date = db.Column(db.Date, nullable=False, index=True)

    # Workforce counts
    total_workforce = db.Column(db.Integer, default=0)
    metex_staff = db.Column(db.Integer, default=0)
    csk_staff = db.Column(db.Integer, default=0)
    topquality_staff = db.Column(db.Integer, default=0)
    bestcare_staff = db.Column(db.Integer, default=0)
    prestige_staff = db.Column(db.Integer, default=0)

    # Machines
    working_machines = db.Column(db.Integer, default=0)
    working_machine_names = db.Column(db.Text, default="")  # comma separated
    out_of_order_machines = db.Column(db.Integer, default=0)
    out_of_order_machine_names = db.Column(db.Text, default="")  # comma separated

    # Staff lists & shift
    workers_on_leave = db.Column(db.Text, default="")  # comma separated
    maintenance_staff = db.Column(db.Text, default="")  # comma separated
    loading_staff = db.Column(db.Text, default="")  # comma separated
    shift = db.Column(db.String(16), default="day")  # "day" or "night"

    updated_at = db.Column(db.DateTime, default=now_uae,
                           onupdate=now_uae)

    def to_dict(self):
        return {
            "id": self.id,
            "record_date": _fmt_date(self.record_date),
            "total_workforce": self.total_workforce,
            "metex_staff": self.metex_staff,
            "csk_staff": self.csk_staff,
            "topquality_staff": self.topquality_staff,
            "bestcare_staff": self.bestcare_staff,
            "prestige_staff": self.prestige_staff,
            "working_machines": self.working_machines,
            "working_machine_names": self.working_machine_names,
            "out_of_order_machines": self.out_of_order_machines,
            "out_of_order_machine_names": self.out_of_order_machine_names,
            "workers_on_leave": self.workers_on_leave,
            "maintenance_staff": self.maintenance_staff,
            "loading_staff": self.loading_staff,
            "shift": self.shift,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _fmt_duration(seconds):
    """Format a duration in seconds as H:MM:SS (or Dd H:MM:SS)."""
    if seconds is None or seconds < 0:
        seconds = 0
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _fmt_date(d):
    """Format a date as dd/mm/yyyy (used everywhere dates are displayed)."""
    if not d:
        return ""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return d
    return d.strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    uid = session["user_id"]
    if uid == 0:  # legacy single-password session
        return {"id": 0, "username": session.get("username", "admin"),
                "display_name": session.get("display_name", "Admin"), "role": "admin"}
    return User.query.get(uid)


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Access control: per-page permissions + admin helpers
# ---------------------------------------------------------------------------
PAGE_KEYS = [
    ("dashboard", "Dashboard"),
    ("machines", "Machines"),
    ("products", "Products"),
    ("groups", "Groups"),
    ("reports", "Reports"),
    ("entry", "Run Console"),
    ("workforce", "Workforce"),
    ("runs", "Production Runs"),
    ("users", "User Management"),
]

# Map each page key to its Flask route endpoint (used to land users on the
# first tab they are allowed to open after login).
PAGE_ROUTES = {
    "dashboard": "dashboard",
    "machines": "machines_page",
    "products": "products_page",
    "groups": "groups_page",
    "reports": "reports_page",
    "entry": "entry",
    "workforce": "workforce_page",
    "runs": "runs_page",
    "users": "users_page",
}

# Roles offered on the login screen, in display order.
LOGIN_ROLES = [
    ("admin", "Administrator", "Full access to every tab and settings"),
    ("supervisor", "Supervisor", "Manage machines, products, runs & reports"),
    ("operator", "Operator", "View dashboard, machines, runs & console"),
    ("viewer", "Viewer", "Read-only monitoring access"),
]


def default_permissions(role):
    """Default page permissions granted to a role."""
    if role == "admin":
        return [k for k, _ in PAGE_KEYS]
    if role == "supervisor":
        return ["dashboard", "machines", "products", "groups",
                "reports", "entry", "workforce", "runs"]
    if role == "viewer":
        return ["dashboard", "machines", "products", "groups",
                "reports", "workforce", "runs"]
    return ["dashboard", "machines", "entry", "runs"]


def is_admin(u):
    if u is None:
        return False
    if isinstance(u, dict):
        return u.get("role") == "admin"
    return u.role == "admin"


def user_can(page_key):
    """Return True if the current user may view the given page key."""
    u = current_user()
    if not u:
        return False
    if is_admin(u):
        return True
    return page_key in (u.permissions_list or [])


def page_required(page_key):
    """Decorator: require the current user to have access to `page_key`."""
    from functools import wraps

    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not user_can(page_key):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco


def admin_required(view):
    """Decorator: require an admin user."""
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin(current_user()):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_helpers():
    return {"user_can": user_can, "PAGE_KEYS": PAGE_KEYS,
            "is_admin_user": is_admin(current_user())}


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    # Step 1 (role selection) is handled client-side; the form always posts
    # role + username + password. We validate the role matches the account.
    if request.method == "POST":
        role = (request.form.get("role") or "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            # The role chosen on the login screen must match the account role.
            if role and role != user.role:
                error = "This account is not registered as that role. Please select the correct role."
            elif not user.is_active:
                error = "This account has been disabled. Contact an administrator."
            else:
                session["user_id"] = user.id
                session["username"] = user.username
                session["display_name"] = user.display_name or user.username
                session["role"] = user.role
                return redirect(url_for("dashboard"))
        # Legacy fallback: shared dashboard password (always admin role)
        elif password == DASHBOARD_PASSWORD:
            session["user_id"] = 0
            session["username"] = username or "admin"
            session["display_name"] = username or "Admin"
            session["role"] = "admin"
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error, LOGIN_ROLES=LOGIN_ROLES)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    """Landing page: show the dashboard if allowed, otherwise the first
    tab the user is permitted to open (so login never dead-ends)."""
    u = current_user()
    if user_can("dashboard"):
        return render_template("dashboard.html", user=u)
    # Find the first page the user is allowed to view and send them there.
    for key in [k for k, _ in PAGE_KEYS]:
        if user_can(key):
            return redirect(url_for(PAGE_ROUTES[key]))
    # Authenticated but no page access at all.
    return render_template("dashboard.html", user=u, no_access=True)


@app.route("/machines")
@login_required
@page_required("machines")
def machines_page():
    return render_template("machines.html", user=current_user())


@app.route("/products")
@login_required
@page_required("products")
def products_page():
    return render_template("products.html", user=current_user())


@app.route("/groups")
@login_required
@page_required("groups")
def groups_page():
    return render_template("groups.html", user=current_user())


@app.route("/reports")
@login_required
@page_required("reports")
def reports_page():
    return render_template("reports.html", user=current_user())


@app.route("/entry")
@login_required
@page_required("entry")
def entry():
    return render_template("entry.html", user=current_user())


@app.route("/workforce")
@login_required
@page_required("workforce")
def workforce_page():
    return render_template("workforce.html", user=current_user())


# ---------------------------------------------------------------------------
# API: summary (dashboard KPIs)
# ---------------------------------------------------------------------------
@app.route("/api/summary")
@login_required
def api_summary():
    date_str = request.args.get("date")
    shift = (request.args.get("shift") or "").strip().lower()
    historical = False
    as_of = None
    if date_str:
        try:
            d = date.fromisoformat(date_str)
            historical = True
            # End of the selected shift on that date.
            if shift == "night":
                as_of = datetime(d.year, d.month, d.day, 7, 0, 0) + timedelta(days=1)
            else:
                as_of = datetime(d.year, d.month, d.day, 19, 0, 0)
        except ValueError:
            pass

    machines = Machine.query.all()
    total = len(machines)
    by_status = {"running": 0, "out_of_order": 0, "maintenance": 0, "idle": 0}

    if historical and as_of:
        # Reconstruct each machine's status as of the end of the chosen shift.
        last_logs = (
            MachineLog.query
            .filter(MachineLog.timestamp <= as_of)
            .order_by(MachineLog.machine_id, MachineLog.timestamp.desc())
            .all()
        )
        status_at = {}
        for l in last_logs:
            status_at.setdefault(l.machine_id, l.status)
        for m in machines:
            st = status_at.get(m.id, "idle")
            by_status[st] = by_status.get(st, 0) + 1
    else:
        for m in machines:
            by_status[m.status] = by_status.get(m.status, 0) + 1

    groups = {g.id: g.name for g in Group.query.all()}
    by_group = {}
    for m in machines:
        gname = groups.get(m.group_id, "Unassigned")
        by_group.setdefault(gname, {"running": 0, "out_of_order": 0, "maintenance": 0, "idle": 0})
        if historical and as_of:
            last = (MachineLog.query.filter_by(machine_id=m.id)
                    .filter(MachineLog.timestamp <= as_of)
                    .order_by(MachineLog.timestamp.desc()).first())
            st = last.status if last else "idle"
        else:
            st = m.status
        by_group[gname][st] = by_group[gname].get(st, 0) + 1

    logs = (MachineLog.query.order_by(MachineLog.timestamp.desc()).limit(8)).all()
    machine_names = {m.id: m.name for m in machines}
    recent_logs = [l.to_dict(machine_name=machine_names.get(l.machine_id)) for l in logs]

    latest_rec = DailyRecord.query.order_by(DailyRecord.record_date.desc()).first()
    workforce_total = latest_rec.total_workforce if latest_rec else 0

    kpis = [
        {"label": "Total Machines", "value": total, "cls": "accent", "type": "total", "trend": None},
        {"label": "Running", "value": by_status.get("running", 0), "cls": "c1", "type": "running", "trend": None},
        {"label": "Break Down", "value": by_status.get("out_of_order", 0), "cls": "c2", "type": "out_of_order", "trend": None},
        {"label": "Maintenance", "value": by_status.get("maintenance", 0), "cls": "c3", "type": "maintenance", "trend": None},
        {"label": "Idle", "value": by_status.get("idle", 0), "cls": "c6", "type": "idle", "trend": None},
        {"label": "Products", "value": Product.query.count(), "cls": "c4", "type": "products", "trend": None},
        {"label": "Groups", "value": Group.query.count(), "cls": "c5", "type": "groups", "trend": None},
        {"label": "Total Workforce", "value": workforce_total, "cls": "accent", "type": "workforce", "trend": None},
    ]

    return jsonify({
        "kpis": kpis,
        "by_status": by_status,
        "by_group": by_group,
        "recent_logs": recent_logs,
        "total": total,
    })


# ---------------------------------------------------------------------------
# API: machines
# ---------------------------------------------------------------------------
@app.route("/api/machines")
@login_required
def api_machines():
    groups = {g.id: g.name for g in Group.query.all()}
    products = {p.id: p.name for p in Product.query.all()}
    machines = Machine.query.order_by(Machine.name).all()
    return jsonify({
        "machines": [m.to_dict(group_name=groups.get(m.group_id),
                               product_name=products.get(m.product_id)) for m in machines]
    })


@app.route("/api/machine/<int:mid>/status", methods=["POST"])
@login_required
def api_machine_status(mid):
    m = Machine.query.get_or_404(mid)
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or m.status).strip().lower()
    if new_status not in ("running", "out_of_order", "maintenance", "idle"):
        return jsonify({"error": "Invalid status"}), 400
    note = str(data.get("note", "")).strip()
    m.status = new_status
    m.notes = note or m.notes
    m.updated_by = session.get("display_name", session.get("username", "Unknown"))
    db.session.add(m)
    log = MachineLog(machine_id=m.id, status=new_status, note=note,
                    updated_by=m.updated_by, shift=shift_of(now_uae()))
    db.session.add(log)
    db.session.commit()
    return jsonify({"machine": m.to_dict(), "log": log.to_dict(machine_name=m.name)})


@app.route("/api/machine/<int:mid>/shift-hours", methods=["POST"])
@login_required
def api_machine_shift_hours(mid):
    """Update planned productive hours for the day/night shift of a machine."""
    m = Machine.query.get_or_404(mid)
    data = request.get_json(silent=True) or {}
    actor = session.get("display_name", session.get("username", "Unknown"))
    changed = False
    for col in ("day_hours", "night_hours"):
        if col in data and data[col] is not None:
            try:
                val = int(data[col])
            except (TypeError, ValueError):
                return jsonify({"error": f"Invalid {col}"}), 400
            if val < 0:
                return jsonify({"error": f"{col} must be >= 0"}), 400
            if getattr(m, col) != val:
                setattr(m, col, val)
                changed = True
    if changed:
        m.updated_by = actor
        m.updated_at = now_uae()
        db.session.add(m)
        db.session.commit()
    return jsonify({"machine": m.to_dict()})


@app.route("/api/machines/bulk-start", methods=["POST"])
@login_required
def api_machines_bulk_start():
    """Start only IDLE machines (records current time).

    Break-down (out_of_order) and maintenance machines are intentionally
    left untouched by the bulk action — they must be changed individually.
    """
    actor = session.get("display_name", session.get("username", "Unknown"))
    now = now_uae()
    count = 0
    for m in Machine.query.filter_by(status="idle").all():
        m.status = "running"
        m.updated_by = actor
        m.updated_at = now
        db.session.add(m)
        db.session.add(MachineLog(machine_id=m.id, status="running",
                                  note="Bulk start (idle machines)", updated_by=actor,
                                  shift=shift_of(now)))
        count += 1
    db.session.commit()
    return jsonify({"updated": count})


@app.route("/api/machines/bulk-stop", methods=["POST"])
@login_required
def api_machines_bulk_stop():
    """Stop only RUNNING machines (records current time).

    Break-down (out_of_order) and maintenance machines are intentionally
    left untouched by the bulk action — they must be changed individually.
    """
    actor = session.get("display_name", session.get("username", "Unknown"))
    now = now_uae()
    count = 0
    for m in Machine.query.filter_by(status="running").all():
        m.status = "idle"
        m.updated_by = actor
        m.updated_at = now
        db.session.add(m)
        db.session.add(MachineLog(machine_id=m.id, status="idle",
                                  note="Bulk stop (running machines)", updated_by=actor,
                                  shift=shift_of(now)))
        count += 1
    db.session.commit()
    return jsonify({"updated": count})


# ---------------------------------------------------------------------------
# API: groups / products / logs
# ---------------------------------------------------------------------------
@app.route("/api/groups")
@login_required
def api_groups():
    groups = Group.query.order_by(Group.name).all()
    out = []
    for g in groups:
        mg = Machine.query.filter_by(group_id=g.id).all()
        by_status = {"running": 0, "out_of_order": 0, "maintenance": 0, "idle": 0}
        for m in mg:
            by_status[m.status] = by_status.get(m.status, 0) + 1
        out.append({
            **g.to_dict(),
            "machine_count": len(mg),
            "by_status": by_status,
            "product_count": Product.query.filter_by(group_id=g.id).count(),
        })
    return jsonify({"groups": out})


@app.route("/api/products")
@login_required
def api_products():
    groups = {g.id: g.name for g in Group.query.all()}
    products = Product.query.order_by(Product.name).all()
    out = []
    for p in products:
        mc = Machine.query.filter_by(product_id=p.id).count()
        out.append({**p.to_dict(), "group_name": groups.get(p.group_id), "machine_count": mc})
    return jsonify({"products": out})


@app.route("/api/machine-logs")
@login_required
def api_logs():
    limit = int(request.args.get("limit", 50))
    logs = MachineLog.query.order_by(MachineLog.timestamp.desc()).limit(limit).all()
    machine_names = {m.id: m.name for m in Machine.query.all()}
    return jsonify({"logs": [l.to_dict(machine_name=machine_names.get(l.machine_id)) for l in logs]})


# ---------------------------------------------------------------------------
# API: reports (status-change history with computed previous status)
# ---------------------------------------------------------------------------
def _build_report_events():
    """Return status-change events with the computed previous status."""
    machine_id = request.args.get("machine_id")
    status_filter = request.args.get("status")  # filter by new status
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = (request.args.get("search") or "").strip().lower()

    q = MachineLog.query
    if machine_id:
        try:
            q = q.filter(MachineLog.machine_id == int(machine_id))
        except ValueError:
            pass
    if date_from:
        try:
            q = q.filter(MachineLog.timestamp >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(MachineLog.timestamp <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    shift = (request.args.get("shift") or "").strip().lower()
    if shift in ("day", "night"):
        q = q.filter(MachineLog.shift == shift)

    logs = q.order_by(MachineLog.timestamp.asc()).all()
    machines = Machine.query.all()
    machine_names = {m.id: m.name for m in machines}
    machine_codes = {m.id: m.code for m in machines}

    last_status = {}
    events = []
    for l in logs:
        from_status = last_status.get(l.machine_id)
        to_status = l.status
        last_status[l.machine_id] = to_status
        name = machine_names.get(l.machine_id, "Unknown")
        code = machine_codes.get(l.machine_id, "")
        if status_filter and to_status != status_filter:
            continue
        if search:
            hay = f"{name} {code} {l.updated_by} {l.note or ''}".lower()
            if search not in hay:
                continue
        events.append({
            "id": l.id,
            "machine_id": l.machine_id,
            "machine_name": name,
            "machine_code": code,
            "from_status": from_status,
            "to_status": to_status,
            "note": l.note or "",
            "updated_by": l.updated_by or "",
            "timestamp": l.timestamp.isoformat() if l.timestamp else None,
        })

    # newest first
    events.reverse()
    return events


@app.route("/api/reports")
@login_required
def api_reports():
    events = _build_report_events()
    # summary of transitions
    summary = {}
    for e in events:
        key = f"{e['from_status'] or 'initial'} → {e['to_status']}"
        summary[key] = summary.get(key, 0) + 1
    return jsonify({"events": events, "summary": summary, "count": len(events)})


@app.route("/api/reports/export")
@login_required
def api_reports_export():
    events = _build_report_events()

    def fmt(ts):
        if not ts:
            return ""
        try:
            return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ts

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Machine", "Code", "From Status",
                     "To Status", "Updated By", "Note"])
    for e in events:
        writer.writerow([
            fmt(e["timestamp"]),
            e["machine_name"],
            e["machine_code"],
            e["from_status"] or "initial",
            e["to_status"],
            e["updated_by"],
            e["note"],
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=machine_status_report.csv"
    return resp


# ---------------------------------------------------------------------------
# API: daily workforce record (legacy + still used by entry form)
# ---------------------------------------------------------------------------
@app.route("/api/record")
@login_required
def get_record():
    """Return the record for a given date + shift (defaults to today / day)."""
    date_str = request.args.get("date")
    shift = (request.args.get("shift") or "day").strip().lower()
    shift = "night" if shift == "night" else "day"
    try:
        target = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    rec = DailyRecord.query.filter_by(record_date=target, shift=shift).first()
    if rec is None:
        return jsonify({"record": None, "record_date": _fmt_date(target), "shift": shift})
    return jsonify({"record": rec.to_dict(), "record_date": _fmt_date(target), "shift": shift})


@app.route("/api/record", methods=["POST"])
@login_required
def upsert_record():
    """Create or update the daily workforce record for a given date + shift."""
    data = request.get_json(silent=True) or {}
    date_str = data.get("record_date") or date.today().isoformat()
    shift = (data.get("shift") or "day").strip().lower()
    shift = "night" if shift == "night" else "day"
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    rec = DailyRecord.query.filter_by(record_date=target, shift=shift).first()
    if rec is None:
        rec = DailyRecord(record_date=target, shift=shift)
        db.session.add(rec)

    int_fields = [
        "total_workforce", "metex_staff", "csk_staff", "topquality_staff",
        "bestcare_staff", "prestige_staff", "working_machines",
        "out_of_order_machines",
    ]
    for f in int_fields:
        if f in data:
            try:
                setattr(rec, f, int(data[f]))
            except (TypeError, ValueError):
                setattr(rec, f, 0)

    for f in ["working_machine_names", "out_of_order_machine_names",
              "workers_on_leave", "maintenance_staff", "loading_staff"]:
        if f in data:
            setattr(rec, f, str(data[f]).strip())

    if "shift" in data:
        shift_val = str(data["shift"]).strip().lower()
        rec.shift = "night" if shift_val == "night" else "day"

    db.session.commit()
    return jsonify({"record": rec.to_dict()})


@app.route("/api/workforce/summary")
@login_required
def api_workforce_summary():
    """Return the latest daily record with parsed staff/machine name lists."""
    rec = DailyRecord.query.order_by(DailyRecord.record_date.desc()).first()
    if not rec:
        return jsonify({"record": None})

    def split(s):
        return [x.strip() for x in (s or "").split(",") if x.strip()]

    return jsonify({
        "record": {
            "record_date": _fmt_date(rec.record_date),
            "shift": rec.shift,
            "total_workforce": rec.total_workforce,
            "metex_staff": rec.metex_staff,
            "csk_staff": rec.csk_staff,
            "topquality_staff": rec.topquality_staff,
            "bestcare_staff": rec.bestcare_staff,
            "prestige_staff": rec.prestige_staff,
            "working_machines": rec.working_machines,
            "out_of_order_machines": rec.out_of_order_machines,
            "working_machine_names": split(rec.working_machine_names),
            "out_of_order_machine_names": split(rec.out_of_order_machine_names),
            "workers_on_leave": split(rec.workers_on_leave),
            "maintenance_staff": split(rec.maintenance_staff),
            "loading_staff": split(rec.loading_staff),
        }
    })


@app.route("/api/history")
@login_required
def history():
    """Return recent daily records (newest first) for the history table."""
    limit = int(request.args.get("limit", 30))
    recs = (DailyRecord.query.order_by(DailyRecord.record_date.desc()).limit(limit)).all()
    return jsonify({"records": [r.to_dict() for r in recs]})


@app.route("/api/dates")
@login_required
def available_dates():
    """Return all dates that have a record (for the date picker)."""
    recs = DailyRecord.query.order_by(DailyRecord.record_date.desc()).all()
    return jsonify({"dates": [r.record_date.isoformat() for r in recs]})


# ---------------------------------------------------------------------------
# User management (admin only) + per-page access control API
# ---------------------------------------------------------------------------
@app.route("/users")
@login_required
@page_required("users")
def users_page():
    return render_template("users.html", user=current_user(), PAGE_KEYS=PAGE_KEYS)


@app.route("/api/users")
@login_required
@admin_required
def api_users_list():
    users = User.query.order_by(User.username).all()
    return jsonify({"users": [u.to_dict() for u in users]})


@app.route("/api/users", methods=["POST"])
@login_required
@admin_required
def api_users_create():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username already exists"}), 400
    role = (data.get("role") or "operator").strip().lower()
    if role not in ("admin", "supervisor", "operator", "viewer"):
        role = "operator"
    perms = data.get("permissions") or default_permissions(role)
    if role == "admin":
        perms = [k for k, _ in PAGE_KEYS]
    u = User(username=username,
             password_hash=generate_password_hash(password),
             display_name=(data.get("display_name") or username).strip(),
             role=role,
             permissions=json.dumps(perms),
             is_active=bool(data.get("is_active", True)))
    db.session.add(u)
    db.session.commit()
    return jsonify({"user": u.to_dict()}), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@login_required
@admin_required
def api_users_update(uid):
    u = User.query.get_or_404(uid)
    data = request.get_json(silent=True) or {}
    if u.role == "admin":
        active_admins = User.query.filter_by(role="admin", is_active=True).count()
        if data.get("role") and data["role"] != "admin" and active_admins <= 1:
            return jsonify({"error": "Cannot change the role of the last admin"}), 400
        if "is_active" in data and not data["is_active"] and active_admins <= 1:
            return jsonify({"error": "Cannot deactivate the last admin"}), 400
    if "display_name" in data:
        u.display_name = str(data["display_name"]).strip() or u.username
    if "role" in data:
        r = str(data["role"]).strip().lower()
        if r in ("admin", "supervisor", "operator", "viewer"):
            u.role = r
    if "is_active" in data:
        u.is_active = bool(data["is_active"])
    if "permissions" in data:
        perms = data["permissions"] or []
        if u.role == "admin":
            perms = [k for k, _ in PAGE_KEYS]
        u.permissions = json.dumps(perms)
    if data.get("password"):
        if len(data["password"]) < 4:
            return jsonify({"error": "Password must be at least 4 characters"}), 400
        u.password_hash = generate_password_hash(data["password"])
    db.session.commit()
    return jsonify({"user": u.to_dict()})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@login_required
@admin_required
def api_users_delete(uid):
    u = User.query.get_or_404(uid)
    if u.role == "admin":
        active_admins = User.query.filter_by(role="admin", is_active=True).count()
        if active_admins <= 1:
            return jsonify({"error": "Cannot delete the last admin"}), 400
    cur = current_user()
    if isinstance(cur, User) and cur.id == u.id:
        return jsonify({"error": "You cannot delete your own account"}), 400
    db.session.delete(u)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Maintenance / seeding
# ---------------------------------------------------------------------------
def _seed():
    """Create default users, groups, products and machines if empty."""
    if User.query.count() == 0:
        db.session.add(User(username="admin",
                            password_hash=generate_password_hash(DASHBOARD_PASSWORD),
                            display_name="Administrator", role="admin",
                            permissions=json.dumps(default_permissions("admin")),
                            is_active=True))
        db.session.add(User(username="supervisor",
                            password_hash=generate_password_hash("super123"),
                            display_name="Supervisor Sam", role="supervisor",
                            permissions=json.dumps(default_permissions("supervisor"))))
        db.session.add(User(username="operator",
                            password_hash=generate_password_hash("oper123"),
                            display_name="Operator Omar", role="operator",
                            permissions=json.dumps(default_permissions("operator"))))
        db.session.commit()

    if Group.query.count() == 0:
        # Real machine names + groups supplied by the user (from machine_names.xlsm).
        machine_groups = [
            ("AB1-TW1", "ANGLE BEAD"), ("AB2-TW2", "ANGLE BEAD"),
            ("AB3-CH1", "ANGLE BEAD"), ("AB4-CH2", "ANGLE BEAD"),
            ("AB5-TR1", "ANGLE BEAD"), ("AB6-TR2", "ANGLE BEAD"),
            ("AB7-TR3", "ANGLE BEAD"),
            ("AR/CJ-TR1", "ARCH-BD/MCJ"),
            ("BR10-W-CH7", "ROLLS"), ("BR11-W-CH8", "ROLLS"),
            ("BR1-UK1", "ROLLS"), ("BR2-CH1", "ROLLS"),
            ("BR3-W-TW1", "ROLLS"), ("BR4-W-CH2", "ROLLS"),
            ("BR5-W-CH3", "ROLLS"), ("BR6-W-TW2", "ROLLS"),
            ("BR7-CH4", "ROLLS"), ("BR8-CH5", "ROLLS"),
            ("BR9-CH6", "ROLLS"),
            ("CL1-CH1", "COIL LATH"), ("CL2-CH2", "COIL LATH"),
            ("CL3-CH3", "COIL LATH"), ("CL4-TW1", "COIL LATH"),
            ("CM1-CH1", "CORNER MESH"),
            ("DECO-GR1", "BENDER SHEET"),
            ("HR1-CH1", "HYRIB"),
            ("LD1-TW1", "LADDER"), ("LD2-GR1", "LADDER"),
            ("LT1-VT1", "LINTEL"), ("LT2-CH1", "LINTEL"),
            ("PS1-TW1", "PLASTER STOP"), ("PS2-TW2", "PLASTER STOP"),
            ("PS3-TR1", "PLASTER STOP"),
            ("SH1-CH1", "SHEET"), ("SH2-CH2", "SHEET"),
            ("SH3-CH3", "SHEET"), ("SH4-CH4", "SHEET"),
            ("WT1-CH1", "WALL TIE"), ("WT2-TW1", "WALL TIE"),
            ("FABRICATION", "PROFILE"),
        ]
        # Create groups (deduplicated, preserving first-seen order).
        group_ids = {}
        for _, gname in machine_groups:
            if gname not in group_ids:
                g = Group(name=gname, description="")
                db.session.add(g)
                db.session.flush()
                group_ids[gname] = g.id
        db.session.commit()

        # Single generic product so the run/entry feature stays functional.
        p1 = Product(name="General", code="GEN-01", group_id=None, target_qty=0, unit="pcs")
        db.session.add(p1)
        db.session.commit()

        for i, (name, gname) in enumerate(machine_groups):
            code = name.replace("/", "-").replace(" ", "")
            # Deterministic status mix so the dashboard demonstrates all states.
            status = "running"
            if i % 9 == 0:
                status = "maintenance"
            elif i % 13 == 0:
                status = "idle"
            db.session.add(Machine(name=name, code=code, group_id=group_ids[gname],
                                   product_id=p1.id, status=status,
                                   location="", updated_by="System"))
        db.session.commit()


def _ensure_columns():
    """Add any newly introduced columns to existing tables (SQLite-safe)."""
    expected = {
        "workers_on_leave": "TEXT",
        "maintenance_staff": "TEXT",
        "loading_staff": "TEXT",
        "shift": "VARCHAR(16)",
    }
    inspector = db.inspect(db.engine)
    existing = {c["name"] for c in inspector.get_columns("daily_records")}
    for col, col_type in expected.items():
        if col not in existing:
            with db.engine.begin() as conn:
                conn.execute(db.text(f"ALTER TABLE daily_records ADD COLUMN {col} {col_type}"))

    # New user columns (permissions + is_active) + backfill for existing users.
    existing_u = {c["name"] for c in inspector.get_columns("users")}
    if "permissions" not in existing_u:
        with db.engine.begin() as conn:
            conn.execute(db.text("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT ''"))
    if "is_active" not in existing_u:
        with db.engine.begin() as conn:
            conn.execute(db.text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"))
    for u in User.query.all():
        if not u.permissions:
            u.permissions = json.dumps(default_permissions(u.role))
        if u.is_active is None:
            u.is_active = True
    db.session.commit()

    # New shift columns on activity tables + per-machine planned shift hours.
    existing_ml = {c["name"] for c in inspector.get_columns("machine_logs")}
    if "shift" not in existing_ml:
        with db.engine.begin() as conn:
            conn.execute(db.text("ALTER TABLE machine_logs ADD COLUMN shift VARCHAR(16) DEFAULT 'day'"))
    existing_pr = {c["name"] for c in inspector.get_columns("production_runs")}
    if "shift" not in existing_pr:
        with db.engine.begin() as conn:
            conn.execute(db.text("ALTER TABLE production_runs ADD COLUMN shift VARCHAR(16) DEFAULT 'day'"))
    existing_m = {c["name"] for c in inspector.get_columns("machines")}
    for col, col_type in (("day_hours", "INTEGER"), ("night_hours", "INTEGER")):
        if col not in existing_m:
            with db.engine.begin() as conn:
                conn.execute(db.text(f"ALTER TABLE machines ADD COLUMN {col} {col_type} DEFAULT 11"))

    # Backfill shift for any existing rows that still have the default 'day'
    # but were actually created at a different time of day.
    for log in MachineLog.query.filter_by(shift="day").all():
        if log.timestamp and shift_of(log.timestamp) == "night":
            log.shift = "night"
    for run in ProductionRun.query.filter_by(shift="day").all():
        if run.started_at and shift_of(run.started_at) == "night":
            run.shift = "night"
    db.session.commit()


def _migrate_db():
    """Rebuild daily_records so (record_date, shift) is the unique key."""
    inspector = db.inspect(db.engine)
    cols = [c["name"] for c in inspector.get_columns("daily_records")]
    indexes = inspector.get_indexes("daily_records")
    has_old_unique = any(
        idx.get("unique") and set(idx.get("column_names", [])) == {"record_date"}
        for idx in indexes
    )
    if not has_old_unique:
        return

    with db.engine.begin() as conn:
        conn.execute(db.text(
            "CREATE TABLE daily_records_new ("
            "id INTEGER PRIMARY KEY, "
            "record_date DATE NOT NULL, "
            "total_workforce INTEGER, metex_staff INTEGER, csk_staff INTEGER, "
            "topquality_staff INTEGER, bestcare_staff INTEGER, prestige_staff INTEGER, "
            "working_machines INTEGER, working_machine_names TEXT, "
            "out_of_order_machines INTEGER, out_of_order_machine_names TEXT, "
            "updated_at DATETIME, workers_on_leave TEXT, "
            "maintenance_staff TEXT, loading_staff TEXT, shift VARCHAR(16), "
            "UNIQUE (record_date, shift))"
        ))
        col_list = ", ".join(cols)
        conn.execute(db.text(
            f"INSERT INTO daily_records_new ({col_list}) SELECT {col_list} FROM daily_records"
        ))
        conn.execute(db.text("DROP TABLE daily_records"))
        conn.execute(db.text("ALTER TABLE daily_records_new RENAME TO daily_records"))


# ---------------------------------------------------------------------------
# HTML: production runs
# ---------------------------------------------------------------------------
@app.route("/runs")
@login_required
@page_required("runs")
def runs_page():
    return render_template("runs.html", user=current_user())


# ---------------------------------------------------------------------------
# API: production runs (start/stop + run-time tracking)
# ---------------------------------------------------------------------------
@app.route("/api/runs")
@login_required
def api_runs():
    machine_id = request.args.get("machine_id")
    status = request.args.get("status")  # running | stopped
    date_str = request.args.get("date")
    shift = (request.args.get("shift") or "").strip().lower()
    q = ProductionRun.query
    if machine_id:
        try:
            q = q.filter(ProductionRun.machine_id == int(machine_id))
        except ValueError:
            pass
    if status:
        q = q.filter(ProductionRun.status == status)
    if date_str:
        try:
            d = date.fromisoformat(date_str)
            q = q.filter(db.func.date(ProductionRun.started_at) == d.isoformat())
        except ValueError:
            pass
    if shift in ("day", "night"):
        q = q.filter(ProductionRun.shift == shift)
    runs = q.order_by(ProductionRun.started_at.desc()).limit(200).all()
    machines = {m.id: m.name for m in Machine.query.all()}
    machine_status = {m.id: m.status for m in Machine.query.all()}
    products = {p.id: p.name for p in Product.query.all()}
    groups = {g.id: g.name for g in Group.query.all()}
    return jsonify({
        "runs": [r.to_dict(machine_name=machines.get(r.machine_id),
                           product_name=products.get(r.product_id),
                           group_name=groups.get(r.group_id),
                           machine_status=machine_status.get(r.machine_id)) for r in runs]
    })


@app.route("/api/run/start", methods=["POST"])
@login_required
def api_run_start():
    data = request.get_json(silent=True) or {}
    machine_id = data.get("machine_id")
    if not machine_id:
        return jsonify({"error": "machine_id required"}), 400
    m = Machine.query.get_or_404(machine_id)
    # close any already-running run for this machine
    open_run = ProductionRun.query.filter_by(machine_id=machine_id, status="running").first()
    if open_run:
        open_run.status = "stopped"
        open_run.stopped_at = now_uae()
        db.session.add(open_run)
    product_id = data.get("product_id") or None
    group_id = data.get("group_id") or m.group_id
    run = ProductionRun(
        machine_id=machine_id,
        product_id=product_id,
        group_id=group_id,
        item_name=str(data.get("item_name") or "").strip(),
        item_code=str(data.get("item_code") or "").strip(),
        operator=session.get("display_name", session.get("username", "Unknown")),
        note=str(data.get("note") or "").strip(),
        status="running",
    )
    db.session.add(run)
    m.status = "running"
    m.updated_by = run.operator
    db.session.add(m)
    db.session.add(MachineLog(machine_id=m.id, status="running",
                              note="Run started" + (f" ({run.item_name})" if run.item_name else ""),
                              updated_by=run.operator, shift=shift_of(run.started_at)))
    db.session.commit()
    return jsonify({"run": run.to_dict(machine_name=m.name)}), 201


@app.route("/api/run/stop/<int:rid>", methods=["POST"])
@login_required
def api_run_stop(rid):
    run = ProductionRun.query.get_or_404(rid)
    if run.status == "running":
        run.status = "stopped"
        run.stopped_at = now_uae()
        payload = request.get_json(silent=True) or {}
        if payload.get("note"):
            run.note = str(payload["note"]).strip()
        db.session.add(run)
        m = Machine.query.get(run.machine_id)
        if m:
            m.status = "idle"
            m.updated_by = session.get("display_name", session.get("username", "Unknown"))
            db.session.add(m)
            db.session.add(MachineLog(machine_id=m.id, status="idle",
                                      note=f"Run stopped ({_fmt_duration(run.run_seconds())})",
                                      updated_by=m.updated_by, shift=shift_of(run.stopped_at)))
        db.session.commit()
    return jsonify({"run": run.to_dict()})


@app.route("/api/runs/export")
@login_required
def api_runs_export():
    date_str = request.args.get("date")
    shift = (request.args.get("shift") or "").strip().lower()
    q = ProductionRun.query
    if date_str:
        try:
            d = date.fromisoformat(date_str)
            q = q.filter(db.func.date(ProductionRun.started_at) == d.isoformat())
        except ValueError:
            pass
    if shift in ("day", "night"):
        q = q.filter(ProductionRun.shift == shift)
    runs = q.order_by(ProductionRun.started_at.desc()).limit(500).all()
    machines = {m.id: m.name for m in Machine.query.all()}
    products = {p.id: p.name for p in Product.query.all()}
    groups = {g.id: g.name for g in Group.query.all()}
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Started", "Stopped", "Run Time", "Machine", "Group",
                     "Product", "Item Name", "Item Code", "Operator", "Status", "Note"])
    for r in runs:
        writer.writerow([
            r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
            r.stopped_at.strftime("%Y-%m-%d %H:%M:%S") if r.stopped_at else "",
            _fmt_duration(r.run_seconds()),
            machines.get(r.machine_id, ""),
            groups.get(r.group_id, ""),
            products.get(r.product_id, ""),
            r.item_name, r.item_code, r.operator, r.status, r.note,
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=production_runs.csv"
    return resp


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        _ensure_columns()
        _seed()
        _migrate_db()
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)