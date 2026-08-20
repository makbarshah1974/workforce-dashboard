#!/usr/bin/env python3
"""
One-time data migration to fix the swapped Group/Product data.

Root cause: the seed data placed the 13 product-types (ANGLE BEAD, ROLLS, ...)
into the `groups` table and the single parent "General" into the `products`
table. The application code already models Group (parent) -> Product (child)
correctly, so only the data needs to be moved between tables and the foreign
keys on `machines` and `production_runs` remapped.

This script:
  1. Backs up the database file.
  2. Creates a real Group named "General".
  3. Moves each product-type row from `groups` into `products`
     (group_id -> the new "General" group).
  4. Remaps machines.group_id -> "General", machines.product_id -> product-type.
  5. Remaps production_runs.group_id -> "General", product_id -> product-type.
  6. Deletes the obsolete old rows.

It is safe to re-run: if the data is already in the correct shape it exits early.
"""
import os
import shutil
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "workforce.db")
BACKUP_PATH = DB_PATH + ".bak_groups_products"

GENERAL_NAME = "General"


def main():
    if not os.path.exists(DB_PATH):
        print("ERROR: database not found at", DB_PATH)
        sys.exit(1)

    # --- Backup -----------------------------------------------------------
    if not os.path.exists(BACKUP_PATH):
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print("Backup created:", BACKUP_PATH)
    else:
        print("Backup already exists:", BACKUP_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- Detect current state --------------------------------------------
    groups = cur.execute("SELECT id, name FROM groups ORDER BY id").fetchall()
    products = cur.execute("SELECT id, name, group_id FROM products ORDER BY id").fetchall()

    # Already-correct shape: exactly one group named "General" and many products.
    if len(groups) == 1 and groups[0]["name"] == GENERAL_NAME and len(products) > 1:
        print("Data already in correct shape (1 group 'General', %d products). "
              "Nothing to do." % len(products))
        conn.close()
        return

    if len(groups) <= 1:
        print("Unexpected state: groups table has %d rows. Aborting." % len(groups))
        conn.close()
        sys.exit(1)

    print("Migrating: %d groups (product-types) -> products, 1 product -> group."
          % len(groups))

    # --- 1. Create the real "General" group ------------------------------
    cur.execute("INSERT INTO groups (name, description) VALUES (?, ?)",
                (GENERAL_NAME, ""))
    general_group_id = cur.lastrowid
    print("Created group '%s' (id=%d)" % (GENERAL_NAME, general_group_id))

    # --- 2. Move each product-type group into products -------------------
    # Map: old group id -> new product id
    old_group_to_product = {}
    for g in groups:
        cur.execute(
            "INSERT INTO products (name, code, group_id, target_qty, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            (g["name"], "PRD-%02d" % g["id"], general_group_id, 0, "pcs"),
        )
        old_group_to_product[g["id"]] = cur.lastrowid
        print("  product '%s' (id=%d) -> group %d"
              % (g["name"], cur.lastrowid, general_group_id))

    # --- 3. Remap machines ----------------------------------------------
    machines = cur.execute("SELECT id, group_id, product_id FROM machines").fetchall()
    for m in machines:
        new_group = general_group_id
        new_product = old_group_to_product.get(m["group_id"], m["product_id"])
        cur.execute(
            "UPDATE machines SET group_id = ?, product_id = ? WHERE id = ?",
            (new_group, new_product, m["id"]),
        )
    print("Remapped %d machines." % len(machines))

    # --- 4. Remap production_runs ----------------------------------------
    runs = cur.execute("SELECT id, group_id, product_id FROM production_runs").fetchall()
    for r in runs:
        new_group = general_group_id
        new_product = old_group_to_product.get(r["group_id"], r["product_id"])
        cur.execute(
            "UPDATE production_runs SET group_id = ?, product_id = ? WHERE id = ?",
            (new_group, new_product, r["id"]),
        )
    print("Remapped %d production_runs." % len(runs))

    # --- 5. Delete obsolete old rows -------------------------------------
    old_group_ids = [g["id"] for g in groups]
    old_product_ids = [p["id"] for p in products]  # the old "General" product row(s)
    cur.execute("DELETE FROM groups WHERE id IN (%s)"
                % ",".join("?" * len(old_group_ids)), old_group_ids)
    if old_product_ids:
        cur.execute("DELETE FROM products WHERE id IN (%s)"
                    % ",".join("?" * len(old_product_ids)), old_product_ids)
    print("Deleted %d old group rows and %d old product rows."
          % (len(old_group_ids), len(old_product_ids)))

    conn.commit()

    # --- Verify -----------------------------------------------------------
    g_after = cur.execute("SELECT id, name FROM groups").fetchall()
    p_after = cur.execute("SELECT id, name, group_id FROM products").fetchall()
    m_check = cur.execute(
        "SELECT COUNT(*) c, MIN(group_id) g FROM machines").fetchone()
    r_check = cur.execute(
        "SELECT COUNT(*) c, MIN(group_id) g FROM production_runs").fetchone()
    print("\n--- After migration ---")
    print("Groups (%d):" % len(g_after), [ (g["id"], g["name"]) for g in g_after ])
    print("Products (%d):" % len(p_after), [ p["name"] for p in p_after ])
    print("Machines: total=%d, all group_id=%s" % (m_check["c"], m_check["g"]))
    print("Runs: total=%d, all group_id=%s" % (r_check["c"], r_check["g"]))

    conn.close()
    print("\nMigration complete. Original data backed up at:", BACKUP_PATH)


if __name__ == "__main__":
    main()