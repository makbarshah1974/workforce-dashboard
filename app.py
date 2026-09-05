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
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# --- Session cookie hardening + proxy fix for Render/mobile ---
# Render terminates TLS and forwards via X-Forwarded-Proto; Flask must
# trust that header or request.is_secure is wrong and Secure cookies break.
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
except Exception:
    pass

# Secure cookies only when served over HTTPS (Render / production DB).
# On plain HTTP LAN (http://192.168.x.x:5000) Secure must be False or
# the session cookie is silently dropped by the browser.
_is_https_env = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))
_db_url_for_cookie = os.environ.get("DATABASE_URL", "")
_is_prod_db = _db_url_for_cookie.startswith("postgres://") or _db_url_for_cookie.startswith("postgresql://")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(_is_https_env or _is_prod_db),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

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
# Cache control — fixes "works only in incognito" on mobile
# Mobile Chrome aggressively caches 302 redirects and HTML. Without
# no-store, normal tabs can loop on a cached / -> /login redirect or
# show a stale /login after you've already logged in. API must never be cached.
# ---------------------------------------------------------------------------
_NO_STORE_PATHS = {"/", "/login", "/dashboard", "/summary", "/machines", "/products", "/groups", "/reports", "/entry", "/workforce", "/runs", "/audit", "/users", "/logout"}


@app.after_request
def _add_cache_headers(resp):
    try:
        p = request.path or ""
        if p.startswith("/api/") or p in _NO_STORE_PATHS:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        elif p.startswith("/static/"):
            # Static assets are versioned via ?v=5 so a short cache is fine.
            # Must override Flask's default "no-cache" for static files.
            resp.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    except Exception:
        pass
    return resp


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

    def to_dict(self, machine_name=None, product_name=None):
        return {
            "id": self.id,
            "machine_id": self.machine_id,
            "machine_name": machine_name,
            "product_name": product_name,
            "status": self.status,
            "note": self.note,
            "updated_by": self.updated_by,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "shift": self.shift,
        }


class UserAuditLog(db.Model):
    """General user-activity audit trail (login/logout, user management, and
    key data changes). Complements MachineLog, which only tracks machine
    status changes. This table answers 'what did a given user do, and when?'."""

    __tablename__ = "user_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    actor = db.Column(db.String(120), default="Unknown")  # denormalized name
    action = db.Column(db.String(40), default="")         # login|logout|user_create|...
    entity_type = db.Column(db.String(32), nullable=True)  # user|machine|run|record
    entity_id = db.Column(db.Integer, nullable=True)
    description = db.Column(db.Text, default="")
    ip_address = db.Column(db.String(45), nullable=True)
    meta = db.Column(db.Text, nullable=True)              # JSON, optional extras
    timestamp = db.Column(db.DateTime, default=now_uae, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "actor": self.actor,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "description": self.description,
            "ip_address": self.ip_address,
            "meta": json.loads(self.meta) if self.meta else None,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
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
    workers_on_leave = db.Column(db.Integer, default=0)  # count
    workers_on_leave_names = db.Column(db.Text, default="")  # comma separated names
    maintenance_staff = db.Column(db.Text, default="")  # comma separated
    loading_staff = db.Column(db.Integer, default=0)  # count
    loading_staff_names = db.Column(db.Text, default="")  # comma separated names
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
            "workers_on_leave": _to_int(self.workers_on_leave),
            "workers_on_leave_names": self.workers_on_leave_names,
            "maintenance_staff": self.maintenance_staff,
            "loading_staff": _to_int(self.loading_staff),
            "loading_staff_names": self.loading_staff_names,
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


def _to_int(v, default=0):
    """Coerce a value to int, tolerating text columns that hold digit strings.

    Some columns were historically stored as TEXT; SQLite keeps the TEXT
    affinity so values come back as strings. This normalizes them to ints.
    """
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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


def current_actor():
    """Best-effort display name of the acting user for the audit trail."""
    u = current_user()
    if isinstance(u, dict):
        return u.get("display_name") or u.get("username") or "admin"
    if u is not None:
        return u.display_name or u.username
    return session.get("display_name") or session.get("username") or "Unknown"


def audit_log(action, entity_type=None, entity_id=None, description="",
              actor=None, ip=None, meta=None, commit=False):
    """Record a user-activity event.

    Best-effort: any failure is swallowed (and the partial add rolled back)
    so auditing can never break the caller's operation. When `commit` is
    False the entry is added to the current session and committed by the
    caller (use this inside endpoints that already commit their changes).
    """
    try:
        if actor is None:
            actor = current_actor()
        if ip is None:
            try:
                ip = request.remote_addr
            except Exception:
                ip = None
        meta_str = None
        if meta is not None:
            try:
                meta_str = json.dumps(meta)
            except (TypeError, ValueError):
                meta_str = None
        u = current_user()
        user_id = u.id if isinstance(u, User) else None
        db.session.add(UserAuditLog(
            user_id=user_id,
            actor=actor or "Unknown",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            description=str(description or ""),
            ip_address=ip,
            meta=meta_str,
        ))
        if commit:
            db.session.commit()
    except Exception as exc:  # pragma: no cover - auditing must never crash
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            app.logger.warning("audit_log failed for action %s: %s", action, exc)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Global error handling: rollback the session on any DB/commit failure and
# return a clean JSON error for API routes (instead of a raw 500 page).
# ---------------------------------------------------------------------------
def _is_api_request():
    return request.path.startswith("/api/")


@app.errorhandler(SQLAlchemyError)
def _handle_sqlalchemy_error(exc):
    db.session.rollback()
    if _is_api_request():
        msg = getattr(exc, "orig", None)
        detail = str(msg) if msg else str(exc)
        return jsonify({"error": "Database error", "detail": detail}), 400
    return jsonify({"error": "Database error"}), 500


@app.errorhandler(Exception)
def _handle_unexpected_error(exc):
    # HTTP exceptions (e.g. 404 from get_or_404) are intentional responses —
    # let Flask render them as-is instead of converting them to 500.
    if isinstance(exc, HTTPException):
        return exc
    # Don't double-handle SQLAlchemy errors (handled above).
    if isinstance(exc, SQLAlchemyError):
        return _handle_sqlalchemy_error(exc)
    db.session.rollback()
    if _is_api_request():
        return jsonify({"error": "Unexpected error", "detail": str(exc)}), 500
    return jsonify({"error": "Unexpected error"}), 500


@app.errorhandler(403)
def _handle_403(exc):
    # API callers get a clean JSON 403.
    if _is_api_request():
        return jsonify({"error": "Forbidden"}), 403
    # Page access denied: silently send the user to the first page they ARE
    # allowed to view instead of showing an error (non-concerned users never
    # see pages they don't have permission for).
    for key in [k for k, _ in PAGE_KEYS]:
        if user_can(key):
            return redirect(url_for(PAGE_ROUTES[key]))
    return redirect(url_for("dashboard"))


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
    ("audit", "Audit Trail"),
    ("summary", "Summary"),
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
    "audit": "audit_page",
    "summary": "summary_page",
}

# Roles offered on the login screen, in display order.
LOGIN_ROLES = [
    ("admin", "Administrator", "Full access to every tab and settings"),
    ("general_manager", "General Manager", "All pages by default; access set by admin"),
    ("operation_manager", "Operation Manager", "All pages by default; access set by admin"),
    ("production_manager", "Production Manager", "All pages by default; access set by admin"),
    ("supervisor", "Supervisor", "Manage machines, products, runs & reports"),
    ("operator", "Operator", "View dashboard, machines, runs & console"),
    ("viewer", "Viewer", "Read-only monitoring access"),
]


def default_permissions(role):
    """Default page permissions granted to a role.

    Manager roles start with access to every page; the admin then narrows
    each individual user's access via the per-user permission toggles.
    """
    if role == "admin":
        return [k for k, _ in PAGE_KEYS]
    if role in ("general_manager", "operation_manager", "production_manager"):
        return [k for k, _ in PAGE_KEYS]
    if role == "supervisor":
        return ["dashboard", "machines", "products", "groups",
                "reports", "entry", "workforce", "runs"]
    if role == "viewer":
        return ["dashboard", "machines", "products", "groups",
                "reports", "workforce", "runs"]
    return ["dashboard", "machines", "entry", "runs"]


# Default permissions per role, used by the client to auto-fill the
# Page Access Control checkboxes. Derived from default_permissions() so the
# client stays in sync whenever a page or role default changes.
ROLE_DEFAULTS = {r: default_permissions(r) for (r, _, _) in LOGIN_ROLES}


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
            "ROLE_DEFAULTS": ROLE_DEFAULTS,
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
                session.permanent = True
                audit_log("login", entity_type="user", entity_id=user.id,
                          description=f"User '{user.username}' logged in",
                          actor=user.display_name or user.username, commit=True)
                return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error, LOGIN_ROLES=LOGIN_ROLES)


@app.route("/logout")
def logout():
    audit_log("logout", description="User logged out", commit=True)
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


@app.route("/summary")
@login_required
@page_required("summary")
def summary_page():
    """Admin-only: machine-wise time summary (run/idle/breakdown/maintenance)
    for a given date + shift, driven by the global filter."""
    return render_template("summary.html", user=current_user())


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
        status_at = _statuses_as_of(as_of) or {}
        for m in machines:
            st = status_at.get(m.id, "idle")
            by_status[st] = by_status.get(st, 0) + 1
    else:
        for m in machines:
            by_status[m.status] = by_status.get(m.status, 0) + 1

    groups = {g.id: g.name for g in Group.query.all()}
    by_group = {}
    # Batched historical statuses instead of per-machine N+1 query
    _g_status_at = _statuses_as_of(as_of) if historical and as_of else None
    for m in machines:
        gname = groups.get(m.group_id, "Unassigned")
        by_group.setdefault(gname, {"running": 0, "out_of_order": 0, "maintenance": 0, "idle": 0})
        if _g_status_at is not None:
            st = _g_status_at.get(m.id, "idle")
        else:
            st = m.status
        by_group[gname][st] = by_group[gname].get(st, 0) + 1

    # Per-product machine status breakdown (for the Dashboard product cards).
    products = {p.id: p.name for p in Product.query.all()}
    by_product = {}
    for m in machines:
        pname = products.get(m.product_id, "Unassigned")
        by_product.setdefault(pname, {"running": 0, "out_of_order": 0, "maintenance": 0, "idle": 0})
        if _g_status_at is not None:
            st = _g_status_at.get(m.id, "idle")
        else:
            st = m.status
        by_product[pname][st] = by_product[pname].get(st, 0) + 1

    logs = (MachineLog.query.order_by(MachineLog.timestamp.desc()).limit(8)).all()
    machine_names = {m.id: m.name for m in machines}
    machine_products = {m.id: products.get(m.product_id, "Unassigned") for m in machines}
    recent_logs = [l.to_dict(
        machine_name=machine_names.get(l.machine_id),
        product_name=machine_products.get(l.machine_id),
    ) for l in logs]

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
        "by_product": by_product,
        "recent_logs": recent_logs,
        "total": total,
    })


# ---------------------------------------------------------------------------
# API: summary/times (admin) — machine-wise accumulated time per status
# ---------------------------------------------------------------------------
def _summary_intervals(from_d, to_d, shift, now):
    """Return a list of (start, end) datetime windows for the date range.

    `shift` is applied per-day:
        day   : 07:00–19:00
        night : 19:00–07:00 (next day)
        (none): whole calendar day
    Any window extending into the future is clamped to `now`; entirely-future
    days are skipped.
    """
    intervals = []
    cur = from_d
    while cur <= to_d:
        if shift == "night":
            s = datetime(cur.year, cur.month, cur.day, 19, 0, 0)
            e = datetime(cur.year, cur.month, cur.day, 7, 0, 0) + timedelta(days=1)
        elif shift == "day":
            s = datetime(cur.year, cur.month, cur.day, 7, 0, 0)
            e = datetime(cur.year, cur.month, cur.day, 19, 0, 0)
        else:
            s = datetime(cur.year, cur.month, cur.day, 0, 0, 0)
            e = datetime(cur.year, cur.month, cur.day, 0, 0, 0) + timedelta(days=1)
        if e > now:
            e = min(e, now)
        if s < now:  # skip days that are entirely in the future
            intervals.append((s, e))
        cur += timedelta(days=1)
    return intervals


def _accumulate_machine(m, intervals, now):
    """Sum seconds per status for one machine across the given intervals."""
    per = {"running": 0, "idle": 0, "out_of_order": 0, "maintenance": 0}
    for (win_start, win_end) in intervals:
        # Status the machine held just before the window started.
        before = (MachineLog.query.filter_by(machine_id=m.id)
                  .filter(MachineLog.timestamp < win_start)
                  .order_by(MachineLog.timestamp.desc()).first())
        # If no log exists before window (new machine), default to idle — not
        # the current live status which would overcount running.
        start_status = before.status if before else "idle"

        logs = (MachineLog.query.filter_by(machine_id=m.id)
                .filter(MachineLog.timestamp >= win_start)
                .filter(MachineLog.timestamp <= win_end)
                .order_by(MachineLog.timestamp.asc()).all())

        # Walk the timeline, accumulating seconds per status.
        cur_status = start_status
        cur_time = win_start
        for l in logs:
            if l.timestamp > cur_time:
                secs = int((l.timestamp - cur_time).total_seconds())
                if cur_status in per:
                    per[cur_status] += secs
            cur_status = l.status
            cur_time = l.timestamp
        if win_end > cur_time:
            secs = int((win_end - cur_time).total_seconds())
            if cur_status in per:
                per[cur_status] += secs
    return per


def _accumulate_machines_batched(machines, intervals, now):
    """Batched version: 2 queries total instead of 2*N*days.

    Returns {machine_id: per_dict}. Correctness identical to _accumulate_machine.
    """
    if not machines or not intervals:
        return {m.id: {"running": 0, "idle": 0, "out_of_order": 0, "maintenance": 0} for m in machines}
    m_ids = [m.id for m in machines]
    m_by_id = {m.id: m for m in machines}
    # Global window
    gmin = min(s for s, _ in intervals)
    gmax = max(e for _, e in intervals)
    # Fetch once: logs before gmin (last per machine) + logs inside [gmin,gmax]
    befores = (MachineLog.query
               .filter(MachineLog.machine_id.in_(m_ids))
               .filter(MachineLog.timestamp < gmin)
               .order_by(MachineLog.machine_id, MachineLog.timestamp.desc())
               .all())
    start_status = {}
    for l in befores:
        start_status.setdefault(l.machine_id, l.status)
    # default to idle if no history
    for mid in m_ids:
        start_status.setdefault(mid, "idle")
    inside = (MachineLog.query
              .filter(MachineLog.machine_id.in_(m_ids))
              .filter(MachineLog.timestamp >= gmin)
              .filter(MachineLog.timestamp <= gmax)
              .order_by(MachineLog.timestamp.asc())
              .all())
    # Group inside logs per machine sorted asc
    from collections import defaultdict
    per_machine_logs = defaultdict(list)
    for l in inside:
        per_machine_logs[l.machine_id].append(l)
    result = {}
    for mid, m in m_by_id.items():
        per = {"running": 0, "idle": 0, "out_of_order": 0, "maintenance": 0}
        logs_sorted = per_machine_logs.get(mid, [])
        # For each window, walk slice of logs
        # Use pointer over sorted logs to avoid re-scanning
        for (win_start, win_end) in intervals:
            # status at window start: walk forward through logs < win_start
            # We can compute by scanning logs_sorted but keep pointer per window
            # Simpler: find start_status for this window — need last log before win_start
            # Use befores + inside-before-window. Walk quickly:
            cur_status = start_status[mid]
            # Advance through logs before win_start to get true status at win_start
            for l in logs_sorted:
                if l.timestamp < win_start:
                    cur_status = l.status
                else:
                    break
            cur_time = win_start
            for l in logs_sorted:
                if l.timestamp < win_start:
                    continue
                if l.timestamp > win_end:
                    break
                if l.timestamp > cur_time:
                    secs = int((l.timestamp - cur_time).total_seconds())
                    if cur_status in per:
                        per[cur_status] += secs
                cur_status = l.status
                cur_time = l.timestamp
            if win_end > cur_time:
                secs = int((win_end - cur_time).total_seconds())
                if cur_status in per:
                    per[cur_status] += secs
        result[mid] = per
    return result


def _parse_summary_params():
    """Parse date range / shift / machine filter from request args.

    Returns (params_dict, error_string). On error error_string is set.
    """
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    date_str = request.args.get("date")  # legacy single-day support
    shift = (request.args.get("shift") or "").strip().lower()
    if shift not in ("day", "night"):
        shift = ""

    if date_from:
        try:
            from_d = date.fromisoformat(date_from)
        except ValueError:
            return None, "Invalid date_from format"
    elif date_str:
        try:
            from_d = date.fromisoformat(date_str)
        except ValueError:
            return None, "Invalid date format"
    else:
        from_d = date.today()

    if date_to:
        try:
            to_d = date.fromisoformat(date_to)
        except ValueError:
            return None, "Invalid date_to format"
    else:
        to_d = from_d

    if to_d < from_d:
        from_d, to_d = to_d, from_d

    # Machine filter (comma-separated ids). Empty = all machines.
    machine_ids = []
    raw_ids = request.args.get("machine_ids")
    if raw_ids:
        for part in raw_ids.split(","):
            part = part.strip()
            if part.isdigit():
                machine_ids.append(int(part))

    return {
        "from_d": from_d,
        "to_d": to_d,
        "shift": shift,
        "machine_ids": machine_ids,
    }, None


def _filtered_machines(params):
    """Return machines for the summary, honoring the machine_ids filter."""
    if params["machine_ids"]:
        return (Machine.query.filter(Machine.id.in_(params["machine_ids"]))
                .order_by(Machine.name).all())
    return Machine.query.order_by(Machine.name).all()


@app.route("/api/summary/times")
@login_required
@page_required("summary")
def api_summary_times():
    """Machine-wise accumulated time per status (running / idle / break down /
    maintenance) for a given date range + shift, computed from the machine
    status log timeline.

    Query params:
        date_from, date_to : inclusive date range (yyyy-mm-dd). Falls back to a
                             single `date=`, then to today.
        shift              : day | night | (empty = whole day)
        machine_ids        : comma-separated machine ids (optional; all if empty)
    """
    params, err = _parse_summary_params()
    if err:
        return jsonify({"error": err}), 400

    now = now_uae()
    intervals = _summary_intervals(params["from_d"], params["to_d"], params["shift"], now)
    machines = _filtered_machines(params)
    groups = {g.id: g.name for g in Group.query.all()}
    products = {p.id: p.name for p in Product.query.all()}

    results = []
    totals = {"running": 0, "idle": 0, "out_of_order": 0, "maintenance": 0}
    # Batched: 2 queries total (was 2*N*days)
    batched = _accumulate_machines_batched(machines, intervals, now) if machines else {}
    for m in machines:
        per = batched.get(m.id) or _accumulate_machine(m, intervals, now)
        for k in totals:
            totals[k] += per[k]
        results.append({
            "machine_id": m.id,
            "machine_name": m.name,
            "machine_code": m.code,
            "group_name": groups.get(m.group_id, "Unassigned"),
            "product_name": products.get(m.product_id, "Unassigned"),
            "running": per["running"],
            "idle": per["idle"],
            "out_of_order": per["out_of_order"],
            "maintenance": per["maintenance"],
            "total": sum(per.values()),
        })

    return jsonify({
        "date_from": _fmt_date(params["from_d"]),
        "date_to": _fmt_date(params["to_d"]),
        "shift": params["shift"] or "all",
        "intervals": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in intervals],
        "machines": results,
        "totals": totals,
    })


@app.route("/api/summary/times/export")
@login_required
@page_required("summary")
def api_summary_times_export():
    """CSV export of the machine-wise time summary (same logic as the JSON
    endpoint above)."""
    params, err = _parse_summary_params()
    if err:
        return jsonify({"error": err}), 400

    now = now_uae()
    intervals = _summary_intervals(params["from_d"], params["to_d"], params["shift"], now)
    machines = _filtered_machines(params)
    groups = {g.id: g.name for g in Group.query.all()}
    products = {p.id: p.name for p in Product.query.all()}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Machine", "Code", "Group", "Product", "Run Time (s)", "Idle Time (s)",
                     "Break Down (s)", "Maintenance (s)", "Total (s)"])
    batched2 = _accumulate_machines_batched(machines, intervals, now) if machines else {}
    for m in machines:
        per = batched2.get(m.id) or _accumulate_machine(m, intervals, now)
        writer.writerow([
            m.name, m.code, groups.get(m.group_id, "Unassigned"),
            products.get(m.product_id, "Unassigned"),
            per["running"], per["idle"], per["out_of_order"],
            per["maintenance"], sum(per.values()),
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=time_summary.csv"
    return resp


@app.route("/api/summary/daywise")
@login_required
@admin_required
def api_summary_daywise():
    """Day-wise accumulated time per status (running / idle / break down /
    maintenance) for a given date range + shift, aggregated across all
    (filtered) machines. One row per calendar day.

    Same filters as the machine-wise summary:
        date_from, date_to : inclusive date range (yyyy-mm-dd)
        shift              : day | night | (empty = whole day)
        machine_ids        : comma-separated machine ids (optional; all if empty)
    """
    params, err = _parse_summary_params()
    if err:
        return jsonify({"error": err}), 400

    now = now_uae()
    # _summary_intervals already yields one (start, end) window per day,
    # clamped to `now`; we reuse it directly so the day boundaries match the
    # machine-wise summary exactly.
    intervals = _summary_intervals(params["from_d"], params["to_d"], params["shift"], now)
    machines = _filtered_machines(params)

    order = ["running", "idle", "out_of_order", "maintenance"]
    days = []
    grand = {k: 0 for k in order}
    for (win_start, win_end) in intervals:
        per = {k: 0 for k in order}
        day_batched = _accumulate_machines_batched(machines, [(win_start, win_end)], now) if machines else {}
        for m in machines:
            mp = day_batched.get(m.id) or _accumulate_machine(m, [(win_start, win_end)], now)
            for k in order:
                per[k] += mp[k]
        day_total = sum(per.values())
        for k in order:
            grand[k] += per[k]
        days.append({
            "date": _fmt_date(win_start.date()),
            "date_iso": win_start.date().isoformat(),
            "running": per["running"],
            "idle": per["idle"],
            "out_of_order": per["out_of_order"],
            "maintenance": per["maintenance"],
            "total": day_total,
        })

    return jsonify({
        "date_from": _fmt_date(params["from_d"]),
        "date_to": _fmt_date(params["to_d"]),
        "shift": params["shift"] or "all",
        "intervals": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in intervals],
        "days": days,
        "totals": grand,
    })


@app.route("/api/summary/daywise/export")
@login_required
@admin_required
def api_summary_daywise_export():
    """CSV export of the day-wise time summary (same logic as the JSON
    endpoint above)."""
    params, err = _parse_summary_params()
    if err:
        return jsonify({"error": err}), 400

    now = now_uae()
    intervals = _summary_intervals(params["from_d"], params["to_d"], params["shift"], now)
    machines = _filtered_machines(params)
    order = ["running", "idle", "out_of_order", "maintenance"]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Run Time (s)", "Idle Time (s)",
                     "Break Down (s)", "Maintenance (s)", "Total (s)"])
    for (win_start, win_end) in intervals:
        per = {k: 0 for k in order}
        day_batched_exp = _accumulate_machines_batched(machines, [(win_start, win_end)], now) if machines else {}
        for m in machines:
            mp = day_batched_exp.get(m.id) or _accumulate_machine(m, [(win_start, win_end)], now)
            for k in order:
                per[k] += mp[k]
        writer.writerow([
            _fmt_date(win_start.date()),
            per["running"], per["idle"], per["out_of_order"],
            per["maintenance"], sum(per.values()),
        ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=daywise_summary.csv"
    return resp


# ---------------------------------------------------------------------------
# API: machines
# ---------------------------------------------------------------------------
def _statuses_as_of(as_of):
    """Return ``{machine_id: status}`` reconstructed as of a timestamp.

    Reuses the most recent MachineLog at or before ``as_of`` for each machine,
    falling back to ``"idle"`` when no log exists yet. Mirrors the historical
    reconstruction in ``api_summary`` so every page agrees on past state.
    """
    if not as_of:
        return None
    last_logs = (
        MachineLog.query
        .filter(MachineLog.timestamp <= as_of)
        .order_by(MachineLog.machine_id, MachineLog.timestamp.desc())
        .all()
    )
    status_at = {}
    for l in last_logs:
        status_at.setdefault(l.machine_id, l.status)
    return status_at


def _resolve_as_of(date_str, shift):
    """Parse ``date`` + ``shift`` query params into an ``as_of`` datetime."""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return None
    if shift == "night":
        return datetime(d.year, d.month, d.day, 7, 0, 0) + timedelta(days=1)
    return datetime(d.year, d.month, d.day, 19, 0, 0)


@app.route("/api/machines")
@login_required
def api_machines():
    as_of = _resolve_as_of(request.args.get("date"),
                           (request.args.get("shift") or "").strip().lower())
    status_at = _statuses_as_of(as_of) if as_of else None
    groups = {g.id: g.name for g in Group.query.all()}
    products = {p.id: p.name for p in Product.query.all()}
    machines = Machine.query.order_by(Machine.name).all()
    # Latest status-change timestamp per machine - drives the live "time in
    # current status" timer on the machine cards (resets to 00:00:00 whenever a
    # status is set/changed). Falls back to updated_at when no log exists yet.
    status_since = {}
    for mid, ts in (db.session.query(MachineLog.machine_id,
                                     db.func.max(MachineLog.timestamp))
                    .group_by(MachineLog.machine_id).all()):
        status_since[mid] = ts.isoformat() if ts else None
    result = []
    for m in machines:
        d = m.to_dict(group_name=groups.get(m.group_id),
                      product_name=products.get(m.product_id))
        if status_at is not None:
            d["status"] = status_at.get(m.id, "idle")
        d["status_since"] = status_since.get(m.id) or (
            m.updated_at.isoformat() if m.updated_at else None)
        result.append(d)
    return jsonify({"machines": result})


def _sync_production_run(machine, new_status, actor, shift, now, note=None):
    """Keep ``production_runs`` consistent with a machine status change.

    - Transition to ``running``: close any already-open run for the machine
      (prevents overlapping runs, mirrors /api/run/start), then open a new
      machine-level run so the Runs page / KPIs / Reports reflect it.
    - Any other status (idle / out_of_order / maintenance): stop the machine's
      currently open run, if any, so a stopped machine never leaves a run
      "running" (which would inflate run time / distort availability).
    """
    open_runs = ProductionRun.query.filter_by(machine_id=machine.id, status="running").all()
    if new_status == "running":
        for r in open_runs:
            r.status = "stopped"
            r.stopped_at = now
            db.session.add(r)
        run = ProductionRun(
            machine_id=machine.id,
            product_id=machine.product_id,
            group_id=machine.group_id,
            operator=actor,
            note=(note or "") if note is not None else "",
            status="running",
            shift=shift,
            started_at=now,
        )
        db.session.add(run)
    else:
        for r in open_runs:
            r.status = "stopped"
            r.stopped_at = now
            db.session.add(r)


@app.route("/api/machine/<int:mid>/status", methods=["POST"])
@login_required
def api_machine_status(mid):
    m = Machine.query.get_or_404(mid)
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or m.status).strip().lower()
    if new_status not in ("running", "out_of_order", "maintenance", "idle"):
        return jsonify({"error": "Invalid status"}), 400
    note = str(data.get("note", "")).strip()
    shift = (data.get("shift") or "").strip().lower()
    if shift not in ("day", "night"):
        shift = shift_of(now_uae())
    old_status = m.status
    m.status = new_status
    m.notes = note  # Always replace notes on status change (empty if not provided)
    m.updated_by = session.get("display_name", session.get("username", "Unknown"))
    db.session.add(m)
    log = MachineLog(machine_id=m.id, status=new_status, note=note,
                    updated_by=m.updated_by, shift=shift)
    db.session.add(log)
    audit_log("machine_status", entity_type="machine", entity_id=m.id,
              description=f"Machine '{m.name}' status {old_status} -> {new_status}"
                         + (f" ({note})" if note else ""),
              meta={"old_status": old_status, "new_status": new_status})
    if old_status != new_status:
        _sync_production_run(m, new_status, m.updated_by, shift, now_uae(), note)
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
        audit_log("machine_status", entity_type="machine", entity_id=m.id,
                  description=f"Machine '{m.name}' planned shift hours updated")
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
        m.notes = ""  # Clear notes on bulk start
        m.updated_by = actor
        m.updated_at = now
        db.session.add(m)
        db.session.add(MachineLog(machine_id=m.id, status="running",
                                  note="Bulk start (idle machines)", updated_by=actor,
                                  shift=shift_of(now)))
        _sync_production_run(m, "running", actor, shift_of(now), now, "Bulk start (idle machines)")
        count += 1
    if count:
        audit_log("machine_bulk", description=f"Bulk start of {count} idle machine(s)",
                  commit=False)
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
        m.notes = ""  # Clear notes on bulk stop
        m.updated_by = actor
        m.updated_at = now
        db.session.add(m)
        db.session.add(MachineLog(machine_id=m.id, status="idle",
                                  note="Bulk stop (running machines)", updated_by=actor,
                                  shift=shift_of(now)))
        _sync_production_run(m, "idle", actor, shift_of(now), now, "Bulk stop (running machines)")
        count += 1
    if count:
        audit_log("machine_bulk", description=f"Bulk stop of {count} running machine(s)",
                  commit=False)
    db.session.commit()
    return jsonify({"updated": count})


@app.route("/api/machines/bulk_update", methods=["POST"])
@login_required
def api_machines_bulk_update():
    """Set status (and optionally shift) for a chosen set of machines at once.

    The Machines page bulk modal posts here with:
        status       - one of running|idle|maintenance|out_of_order
        shift        - optional "day"|"night" (or "" to keep current)
        machine_ids  - list of machine ids to update
        update_runs  - accepted for backwards compatibility; production runs
                       are now kept in sync on every status change (a
                       transition to "running" opens a run; any other status
                       stops the machine's open run).
    """
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in ("running", "out_of_order", "maintenance", "idle"):
        return jsonify({"error": "Invalid status"}), 400
    shift = (data.get("shift") or "").strip().lower()
    if shift and shift not in ("day", "night"):
        return jsonify({"error": "Invalid shift"}), 400
    machine_ids = data.get("machine_ids") or []
    try:
        machine_ids = [int(x) for x in machine_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid machine ids"}), 400
    if not machine_ids:
        return jsonify({"error": "No machines selected"}), 400
    update_runs = bool(data.get("update_runs", False))
    bulk_note = data.get("note", "").strip()  # Optional note for all machines
    actor = session.get("display_name", session.get("username", "Unknown"))
    now = now_uae()
    count = 0
    for m in Machine.query.filter(Machine.id.in_(machine_ids)).all():
        old_status = m.status
        m.status = status
        m.notes = bulk_note  # Apply bulk note to all (empty if not provided)
        m.updated_by = actor
        m.updated_at = now
        db.session.add(m)
        # Use bulk_note in log if provided, otherwise default message
        log_note = bulk_note if bulk_note else "Bulk update"
        db.session.add(MachineLog(
            machine_id=m.id, status=status, note=log_note,
            updated_by=actor, shift=shift or shift_of(now)))
        if old_status != status:
            _sync_production_run(m, status, actor, shift or shift_of(now), now, log_note)
        count += 1
    if count:
        audit_log("machine_bulk",
                  description=f"Bulk status update to '{status}' on {count} machine(s)",
                  commit=False)
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
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid limit"}), 400
    logs = MachineLog.query.order_by(MachineLog.timestamp.desc()).limit(limit).all()
    machines = Machine.query.all()
    machine_names = {m.id: m.name for m in machines}
    products = {p.id: p.name for p in Product.query.all()}
    machine_products = {m.id: products.get(m.product_id, "Unassigned") for m in machines}
    return jsonify({"logs": [l.to_dict(
        machine_name=machine_names.get(l.machine_id),
        product_name=machine_products.get(l.machine_id),
    ) for l in logs]})


# ---------------------------------------------------------------------------
# API: user audit trail (full activity log + per-user "trail")
# ---------------------------------------------------------------------------
# Human-readable labels for the audit `action` values (filter dropdown +
# row rendering in the Audit Trail page).
AUDIT_ACTION_LABELS = {
    "login": "Login",
    "logout": "Logout",
    "user_create": "User created",
    "user_update": "User updated",
    "user_delete": "User deleted",
    "machine_status": "Machine status",
    "machine_bulk": "Machine bulk update",
    "run_start": "Run started",
    "run_stop": "Run stopped",
    "record_upsert": "Workforce record",
}


def _apply_audit_filters(q):
    """Apply query-string filters to a UserAuditLog query.

    Filters: user (actor, fuzzy), action, entity_type, search (description),
    date_from, date_to. Raises ValueError on malformed dates.
    """
    user = (request.args.get("user") or "").strip()
    if user:
        q = q.filter(UserAuditLog.actor.ilike(f"%{user}%"))
    action = (request.args.get("action") or "").strip()
    if action:
        q = q.filter(UserAuditLog.action == action)
    entity_type = (request.args.get("entity_type") or "").strip()
    if entity_type:
        q = q.filter(UserAuditLog.entity_type == entity_type)
    search = (request.args.get("search") or "").strip().lower()
    if search:
        q = q.filter(UserAuditLog.description.ilike(f"%{search}%"))
    date_from = request.args.get("date_from")
    if date_from:
        try:
            q = q.filter(UserAuditLog.timestamp >= datetime.fromisoformat(date_from))
        except ValueError:
            raise ValueError("Invalid date_from")
    date_to = request.args.get("date_to")
    if date_to:
        try:
            end = datetime.fromisoformat(date_to)
            q = q.filter(UserAuditLog.timestamp < end + timedelta(days=1))
        except ValueError:
            raise ValueError("Invalid date_to")
    return q


@app.route("/api/audit-logs")
@login_required
@page_required("audit")
def api_audit_logs():
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid limit"}), 400
    if limit <= 0 or limit > 500:
        limit = 100
    try:
        q = _apply_audit_filters(UserAuditLog.query)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    logs = q.order_by(UserAuditLog.timestamp.desc()).limit(limit).all()
    return jsonify({
        "logs": [l.to_dict() for l in logs],
        "count": len(logs),
        "actions": AUDIT_ACTION_LABELS,
    })


@app.route("/api/audit-logs/export")
@login_required
@page_required("audit")
def api_audit_logs_export():
    import csv
    import io
    try:
        q = _apply_audit_filters(UserAuditLog.query)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    logs = q.order_by(UserAuditLog.timestamp.desc()).limit(10000).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Time", "User", "Action", "Entity", "Entity ID",
                     "Description", "IP"])
    for l in logs:
        writer.writerow([
            l.timestamp.isoformat() if l.timestamp else "",
            l.actor or "",
            AUDIT_ACTION_LABELS.get(l.action, l.action),
            l.entity_type or "",
            l.entity_id if l.entity_id is not None else "",
            l.description or "",
            l.ip_address or "",
        ])
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=audit_log.csv"
    return resp


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
            raise ValueError("Invalid machine_id")
    if date_from:
        try:
            q = q.filter(MachineLog.timestamp >= datetime.fromisoformat(date_from))
        except ValueError:
            raise ValueError("Invalid date_from")
    if date_to:
        try:
            end = datetime.fromisoformat(date_to)
            q = q.filter(MachineLog.timestamp < end + timedelta(days=1))
        except ValueError:
            raise ValueError("Invalid date_to")
    shift = (request.args.get("shift") or "").strip().lower()
    if shift in ("day", "night"):
        q = q.filter(MachineLog.shift == shift)

    logs = q.order_by(MachineLog.timestamp.asc()).all()
    machines = Machine.query.all()
    machine_names = {m.id: m.name for m in machines}
    machine_codes = {m.id: m.code for m in machines}
    products = {p.id: p.name for p in Product.query.all()}
    machine_products = {m.id: products.get(m.product_id, "Unassigned") for m in machines}

    # Next status-change timestamp per machine -> duration the held status lasted.
    next_ts = {}
    by_machine = {}
    for l in logs:
        by_machine.setdefault(l.machine_id, []).append(l)
    for lst in by_machine.values():
        for i, l in enumerate(lst):
            nxt = lst[i + 1].timestamp if i + 1 < len(lst) else None
            next_ts[l.id] = nxt

    last_status = {}
    events = []
    status_totals = {}
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
        nxt = next_ts[l.id]
        duration = int((nxt - l.timestamp).total_seconds()) if nxt else None
        if duration is not None:
            status_totals[to_status] = status_totals.get(to_status, 0) + duration
        events.append({
            "id": l.id,
            "machine_id": l.machine_id,
            "machine_name": name,
            "machine_code": code,
            "product_name": machine_products.get(l.machine_id, "Unassigned"),
            "from_status": from_status,
            "to_status": to_status,
            "shift": l.shift or "",
            "duration_seconds": duration,
            "note": l.note or "",
            "updated_by": l.updated_by or "",
            "timestamp": l.timestamp.isoformat() if l.timestamp else None,
        })

    # newest first
    events.reverse()
    return events, status_totals


@app.route("/api/reports")
@login_required
def api_reports():
    try:
        events, status_totals = _build_report_events()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # summary of transitions
    summary = {}
    for e in events:
        key = f"{e['from_status'] or 'initial'} → {e['to_status']}"
        summary[key] = summary.get(key, 0) + 1
    return jsonify({"events": events, "summary": summary,
                    "count": len(events), "status_totals": status_totals})


@app.route("/api/reports/export")
@login_required
def api_reports_export():
    try:
        events, _ = _build_report_events()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    def fmt(ts):
        if not ts:
            return ""
        try:
            return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ts

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Machine", "Code", "Product", "From Status",
                     "To Status", "Shift", "Duration", "Updated By", "Note"])
    for e in events:
        writer.writerow([
            fmt(e["timestamp"]),
            e["machine_name"],
            e["machine_code"],
            e["product_name"],
            e["from_status"] or "initial",
            e["to_status"],
            e["shift"] or "",
            _fmt_duration(e["duration_seconds"]) if e["duration_seconds"] is not None else "",
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
        "out_of_order_machines", "workers_on_leave", "loading_staff",
    ]
    for f in int_fields:
        if f in data:
            try:
                setattr(rec, f, int(data[f]))
            except (TypeError, ValueError):
                return jsonify({"error": f"Field '{f}' must be a whole number"}), 400

    for f in ["working_machine_names", "out_of_order_machine_names",
              "workers_on_leave_names", "maintenance_staff", "loading_staff_names"]:
        if f in data:
            setattr(rec, f, str(data[f]).strip())

    if "shift" in data:
        shift_val = str(data["shift"]).strip().lower()
        rec.shift = "night" if shift_val == "night" else "day"

    db.session.commit()
    audit_log("record_upsert", entity_type="record", entity_id=rec.id,
              description=f"Workforce record saved for {_fmt_date(rec.record_date)} ({rec.shift})",
              commit=True)
    return jsonify({"record": rec.to_dict()})


@app.route("/api/workforce/summary")
@login_required
def api_workforce_summary():
    """Return the daily record for a given date + shift (defaults to latest)."""
    date_str = request.args.get("date")
    shift = (request.args.get("shift") or "").strip().lower()
    shift = "night" if shift == "night" else "day"
    q = DailyRecord.query
    if date_str:
        try:
            target = date.fromisoformat(date_str)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400
        q = q.filter_by(record_date=target)
    if shift:
        q = q.filter_by(shift=shift)
    rec = q.order_by(DailyRecord.record_date.desc()).first()
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
            "workers_on_leave": _to_int(rec.workers_on_leave),
            "workers_on_leave_names": split(rec.workers_on_leave_names),
            "maintenance_staff": split(rec.maintenance_staff),
            "loading_staff": _to_int(rec.loading_staff),
            "loading_staff_names": split(rec.loading_staff_names),
        }
    })


@app.route("/api/history")
@login_required
def history():
    """Return daily records for the history table.

    By default it returns the most recent ``limit`` records (newest first).
    If ``month`` (YYYY-MM) is supplied it returns ALL records within that
    calendar month (newest first) -- this is what the Workforce page uses to
    show the current month's history. A single ``date`` filter is still
    honoured when no month is given.
    """
    shift = (request.args.get("shift") or "").strip().lower()
    shift = "night" if shift == "night" else ("day" if shift == "day" else "")

    # ---- Optional month filter (current-month history) ----
    month_str = (request.args.get("month") or "").strip()
    month_start = month_end = None
    if month_str:
        try:
            month_start = datetime.strptime(month_str, "%Y-%m").date()
        except ValueError:
            return jsonify({"error": "Invalid month format, expected YYYY-MM"}), 400
        if month_start.month == 12:
            month_end = date(month_start.year + 1, 1, 1)
        else:
            month_end = date(month_start.year, month_start.month + 1, 1)

    q = DailyRecord.query
    if month_start:
        q = q.filter(DailyRecord.record_date >= month_start,
                     DailyRecord.record_date < month_end)
    else:
        date_str = request.args.get("date")
        if date_str:
            try:
                target = date.fromisoformat(date_str)
            except ValueError:
                return jsonify({"error": "Invalid date format"}), 400
            q = q.filter_by(record_date=target)

    if shift:
        q = q.filter_by(shift=shift)

    if month_start:
        recs = q.order_by(DailyRecord.record_date.desc()).all()
    else:
        try:
            limit = int(request.args.get("limit", 30))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid limit"}), 400
        recs = q.order_by(DailyRecord.record_date.desc()).limit(limit).all()

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


@app.route("/audit")
@login_required
@page_required("audit")
def audit_page():
    return render_template("audit.html", user=current_user())


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
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username already exists"}), 400
    role = (data.get("role") or "operator").strip().lower()
    if role not in ("admin", "supervisor", "operator", "viewer",
                    "general_manager", "operation_manager", "production_manager"):
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
    audit_log("user_create", entity_type="user", entity_id=u.id,
              description=f"Created user '{u.username}' (role: {u.role})",
              actor=current_actor(), commit=True)
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
    if "username" in data:
        new_username = str(data["username"]).strip()
        if not new_username:
            return jsonify({"error": "Username cannot be empty"}), 400
        if User.query.filter(User.username == new_username, User.id != u.id).first():
            return jsonify({"error": "Username already exists"}), 400
        u.username = new_username
    if "display_name" in data:
        u.display_name = str(data["display_name"]).strip() or u.username
    if "role" in data:
        r = str(data["role"]).strip().lower()
        if r in ("admin", "supervisor", "operator", "viewer",
                 "general_manager", "operation_manager", "production_manager"):
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
    audit_log("user_update", entity_type="user", entity_id=u.id,
              description=f"Updated user '{u.username}' (role: {u.role}, active: {u.is_active})",
              actor=current_actor(), commit=True)
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
    deleted_name = u.username
    deleted_id = u.id
    db.session.delete(u)
    db.session.commit()
    audit_log("user_delete", entity_type="user", entity_id=deleted_id,
              description=f"Deleted user '{deleted_name}'",
              actor=current_actor(), commit=True)
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
        "workers_on_leave": "INTEGER",
        "workers_on_leave_names": "TEXT",
        "maintenance_staff": "TEXT",
        "loading_staff": "INTEGER",
        "loading_staff_names": "TEXT",
        "shift": "VARCHAR(16)",
    }
    inspector = db.inspect(db.engine)
    existing = {c["name"] for c in inspector.get_columns("daily_records")}
    for col, col_type in expected.items():
        if col not in existing:
            with db.engine.begin() as conn:
                conn.execute(db.text(f"ALTER TABLE daily_records ADD COLUMN {col} {col_type}"))

    # One-time data migration: the old schema stored comma-separated NAMES in
    # workers_on_leave / loading_staff. Convert those into a count (int) plus a
    # separate names column. Rows that already hold a pure number are left as-is;
    # blank values are normalized to 0 so the Integer columns stay clean.
    with db.engine.begin() as conn:
        rows = conn.execute(db.text(
            "SELECT id, workers_on_leave, loading_staff FROM daily_records"
        )).fetchall()
        for rid, wol, lod in rows:
            sets, params = [], {"id": rid}
            wol_s = str(wol).strip() if wol is not None else ""
            if wol_s and not wol_s.isdigit():
                names = [x.strip() for x in wol_s.split(",") if x.strip()]
                sets.append("workers_on_leave = :wol")
                sets.append("workers_on_leave_names = :wol_names")
                params["wol"] = len(names)
                params["wol_names"] = ", ".join(names)
            elif not wol_s:
                sets.append("workers_on_leave = :wol")
                params["wol"] = 0
            lod_s = str(lod).strip() if lod is not None else ""
            if lod_s and not lod_s.isdigit():
                names = [x.strip() for x in lod_s.split(",") if x.strip()]
                sets.append("loading_staff = :lod")
                sets.append("loading_staff_names = :lod_names")
                params["lod"] = len(names)
                params["lod_names"] = ", ".join(names)
            elif not lod_s:
                sets.append("loading_staff = :lod")
                params["lod"] = 0
            if sets:
                conn.execute(db.text(
                    f"UPDATE daily_records SET {', '.join(sets)} WHERE id = :id"
                ), params)

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
            "updated_at DATETIME, workers_on_leave INTEGER, "
            "workers_on_leave_names TEXT, "
            "maintenance_staff TEXT, loading_staff INTEGER, "
            "loading_staff_names TEXT, shift VARCHAR(16), "
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
    date_str = request.args.get("date")  # legacy single-day support
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    shift = (request.args.get("shift") or "").strip().lower()
    machine_status_filter = request.args.get("machine_status")
    q = ProductionRun.query
    if machine_id:
        try:
            q = q.filter(ProductionRun.machine_id == int(machine_id))
        except ValueError:
            return jsonify({"error": "Invalid machine_id"}), 400
    if status:
        q = q.filter(ProductionRun.status == status)
    if machine_status_filter:
        q = q.join(Machine).filter(Machine.status == machine_status_filter)
    if date_from:
        try:
            d = date.fromisoformat(date_from)
            q = q.filter(db.func.date(ProductionRun.started_at) >= d.isoformat())
        except ValueError:
            return jsonify({"error": "Invalid date_from"}), 400
    if date_to:
        try:
            d = date.fromisoformat(date_to)
            q = q.filter(db.func.date(ProductionRun.started_at) <= d.isoformat())
        except ValueError:
            return jsonify({"error": "Invalid date_to"}), 400
    if date_str:
        try:
            d = date.fromisoformat(date_str)
            q = q.filter(db.func.date(ProductionRun.started_at) == d.isoformat())
        except ValueError:
            return jsonify({"error": "Invalid date"}), 400
    if shift in ("day", "night"):
        q = q.filter(ProductionRun.shift == shift)
    # Raise the row cap when a narrowing filter is applied so date ranges
    # (and other filters) aren't truncated.
    has_filter = any([machine_id, status, date_str, date_from, date_to, shift])
    runs = q.order_by(ProductionRun.started_at.desc()).limit(2000     if has_filter else 200).all()
    products = {p.id: p.name for p in Product.query.all()}
    groups = {g.id: g.name for g in Group.query.all()}
    all_machines = Machine.query.all()
    machines = {m.id: m.name for m in all_machines}
    machine_status = {m.id: m.status for m in all_machines}
    # Fall back to the machine's own product/group when a run wasn't stored
    # with one (e.g. legacy runs, bulk/status runs before product tracking,
    # or console starts left on "— none —").
    machine_product = {m.id: products.get(m.product_id) for m in all_machines}
    machine_group = {m.id: groups.get(m.group_id) for m in all_machines}
    return jsonify({
        "runs": [r.to_dict(machine_name=machines.get(r.machine_id),
                           product_name=products.get(r.product_id) or machine_product.get(r.machine_id),
                           group_name=groups.get(r.group_id) or machine_group.get(r.machine_id),
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
    if product_id is not None:
        if not Product.query.get(product_id):
            return jsonify({"error": "Invalid product_id"}), 400
    group_id = data.get("group_id") or m.group_id
    if group_id is not None:
        if not Group.query.get(group_id):
            return jsonify({"error": "Invalid group_id"}), 400
    shift = (data.get("shift") or "").strip().lower()
    if shift not in ("day", "night"):
        shift = shift_of(now_uae())
    run = ProductionRun(
        machine_id=machine_id,
        product_id=product_id,
        group_id=group_id,
        item_name=str(data.get("item_name") or "").strip(),
        item_code=str(data.get("item_code") or "").strip(),
        operator=session.get("display_name", session.get("username", "Unknown")),
        note=str(data.get("note") or "").strip(),
        status="running",
        shift=shift,
    )
    db.session.add(run)
    m.status = "running"
    m.updated_by = run.operator
    db.session.add(m)
    db.session.add(MachineLog(machine_id=m.id, status="running",
                              note="Run started" + (f" ({run.item_name})" if run.item_name else ""),
                              updated_by=run.operator, shift=shift))
    audit_log("run_start", entity_type="machine", entity_id=m.id,
              description=f"Production run started on '{m.name}'"
                         + (f" ({run.item_name})" if run.item_name else ""),
              meta={"run_id": run.id})
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
        shift = (payload.get("shift") or "").strip().lower()
        if shift not in ("day", "night"):
            shift = shift_of(run.stopped_at)
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
                                      updated_by=m.updated_by, shift=shift))
            audit_log("run_stop", entity_type="machine", entity_id=m.id,
                      description=f"Production run #{run.id} stopped on '{m.name}'"
                                 f" ({_fmt_duration(run.run_seconds())})",
                      meta={"run_id": run.id})
        db.session.commit()
    return jsonify({"run": run.to_dict()})


@app.route("/api/run/<int:rid>", methods=["PUT"])
@login_required
def api_run_update(rid):
    """Edit a production run's entry date/time (and status/note) after the fact.

    Used by the Runs page "Edit" modal to correct a run's started_at /
    stopped_at when they were recorded incorrectly. This is an "entry"-level
    correction, so it is restricted to users with the `entry` page permission.
    """
    if not user_can("entry"):
        abort(403)
    run = ProductionRun.query.get_or_404(rid)
    data = request.get_json(silent=True) or {}

    # --- started_at (required, must be a valid datetime) ---
    started_raw = (data.get("started_at") or "").strip()
    if not started_raw:
        return jsonify({"error": "started_at is required"}), 400
    try:
        started_at = datetime.fromisoformat(started_raw)
    except ValueError:
        return jsonify({"error": "Invalid started_at format"}), 400

    # --- stopped_at (optional; blank keeps the run running) ---
    stopped_raw = (data.get("stopped_at") or "").strip()
    stopped_at = None
    if stopped_raw:
        try:
            stopped_at = datetime.fromisoformat(stopped_raw)
        except ValueError:
            return jsonify({"error": "Invalid stopped_at format"}), 400
        if stopped_at < started_at:
            return jsonify({"error": "Stop time cannot be before start time"}), 400

    run.started_at = started_at
    run.stopped_at = stopped_at
    # Recompute the shift from the (possibly corrected) start time.
    run.shift = shift_of(started_at)

    # --- status (optional) ---
    status = (data.get("status") or "").strip().lower()
    if status in ("running", "stopped"):
        # Keep status consistent with whether a stop time was supplied.
        if status == "stopped" and stopped_at is None:
            stopped_at = now_uae()
            run.stopped_at = stopped_at
            run.shift = shift_of(stopped_at)
        if status == "running":
            run.stopped_at = None
        run.status = status

    if "note" in data:
        run.note = str(data["note"]).strip()

    db.session.add(run)
    db.session.commit()
    return jsonify({"run": run.to_dict()})

@app.route("/api/run/<int:rid>", methods=["DELETE"])
@login_required
def api_run_delete(rid):
    """Delete a production run.

    This is a correction available to users with the `entry` page permission
    (the same gate as editing a run). Viewers and any user without `entry`
    receive a 403.
    """
    if not user_can("entry"):
        abort(403)
    run = ProductionRun.query.get_or_404(rid)
    db.session.delete(run)
    db.session.commit()
    return jsonify({"ok": True, "id": rid})




@app.route("/api/runs/export")
@login_required
def api_runs_export():
    machine_id = request.args.get("machine_id")
    status = request.args.get("status")  # running | stopped
    date_str = request.args.get("date")  # legacy single-day support
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    shift = (request.args.get("shift") or "").strip().lower()
    machine_status_filter = request.args.get("machine_status")
    q = ProductionRun.query
    if machine_id:
        try:
            q = q.filter(ProductionRun.machine_id == int(machine_id))
        except ValueError:
            return jsonify({"error": "Invalid machine_id"}), 400
    if status:
        q = q.filter(ProductionRun.status == status)
    if machine_status_filter:
        q = q.join(Machine).filter(Machine.status == machine_status_filter)
    if date_from:
        try:
            d = date.fromisoformat(date_from)
            q = q.filter(db.func.date(ProductionRun.started_at) >= d.isoformat())
        except ValueError:
            return jsonify({"error": "Invalid date_from"}), 400
    if date_to:
        try:
            d = date.fromisoformat(date_to)
            q = q.filter(db.func.date(ProductionRun.started_at) <= d.isoformat())
        except ValueError:
            return jsonify({"error": "Invalid date_to"}), 400
    if date_str:
        try:
            d = date.fromisoformat(date_str)
            q = q.filter(db.func.date(ProductionRun.started_at) == d.isoformat())
        except ValueError:
            return jsonify({"error": "Invalid date"}), 400
    if shift in ("day", "night"):
        q = q.filter(ProductionRun.shift == shift)
    runs = q.order_by(ProductionRun.started_at.desc()).all()
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


def _lan_ip():
    """Best-effort detection of this machine's LAN IPv4 for LAN access hints."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        _ensure_columns()
        _seed()
        _migrate_db()
    lan = _lan_ip()
    print("\n" + "=" * 60)
    print("Workforce Dashboard is running.")
    print(f"  This PC (localhost):  http://127.0.0.1:{PORT}")
    print(f"  Other PCs / phones on same Wi-Fi:  http://{lan}:{PORT}")
    print("  (Make sure Windows Firewall allows Python on port", PORT, ")")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
