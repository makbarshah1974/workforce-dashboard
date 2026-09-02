"""WSGI entry point used by Gunicorn on Render (and production hosts).

Local dev runs `python app.py`, which creates the schema and seeds default
data inside its `if __name__ == "__main__"` block. Gunicorn imports this module
instead of running that block, so we run the exact same initialization here
once at startup. Importing this module is a side-effect-free no-op on repeat
imports, but the deployment start command calls `init_db()` explicitly once
before starting the server.
"""
from app import (
    _ensure_columns,
    _migrate_db,
    _seed,
    app,
    db,
)


def init_db():
    """Create/upgrade the schema and seed defaults — identical to what
    `python app.py` does (db.create_all -> ensure columns -> migrate ->
    seed). Runs inside an app context so table creation auto-applies.
    """
    with app.app_context():
        db.create_all()
        _ensure_columns()
        _migrate_db()
        _seed()


# `application` is what Gunicorn/Gunicorn-compatible servers import by
# default; Render's standard start command can point at `app:app`, but
# exposing it under the conventional name makes the Procfile unambiguous.
application = app