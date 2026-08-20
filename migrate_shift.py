from app import app, _ensure_columns, _migrate_db

with app.app_context():
    _ensure_columns()
    _migrate_db()
    print("migration done")