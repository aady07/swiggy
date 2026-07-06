from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import aiosqlite

from backend.config import settings
from backend.models import DraftItem, DraftStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id TEXT NOT NULL,
  raw_query TEXT,
  resolved_sku TEXT,
  resolved_name TEXT,
  pack_size TEXT,
  unit_price REAL,
  merged_qty INTEGER DEFAULT 1,
  requested_by TEXT,
  status TEXT,
  alternatives TEXT,
  clarification_options TEXT,
  auto_picked INTEGER DEFAULT 0,
  product_id TEXT,
  excluded INTEGER DEFAULT 0,
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS resolved_catalog_cache (
  raw_query TEXT PRIMARY KEY,
  sku TEXT,
  name TEXT,
  last_checked TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id TEXT,
  items_json TEXT,
  total REAL,
  placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id TEXT,
  sender TEXT,
  text TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  access_token TEXT,
  expires_at TIMESTAMP,
  client_id TEXT
);

CREATE TABLE IF NOT EXISTS oauth_pending (
  state TEXT PRIMARY KEY,
  code_verifier TEXT,
  client_id TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS price_watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id TEXT NOT NULL,
  spin_id TEXT NOT NULL,
  name TEXT,
  search_query TEXT,
  product_id TEXT,
  pack_size TEXT,
  last_paid_price REAL,
  last_seen_offer REAL,
  last_seen_mrp REAL,
  last_ordered_at TEXT,
  last_alert_at TEXT,
  muted INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(household_id, spin_id)
);

CREATE TABLE IF NOT EXISTS deal_alert_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id TEXT NOT NULL,
  spin_id TEXT NOT NULL,
  alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_draft(row: aiosqlite.Row) -> DraftItem:
    return DraftItem(
        id=row["id"],
        household_id=row["household_id"],
        raw_query=row["raw_query"] or "",
        resolved_sku=row["resolved_sku"],
        resolved_name=row["resolved_name"],
        pack_size=row["pack_size"],
        unit_price=row["unit_price"],
        merged_qty=row["merged_qty"] or 1,
        requested_by=json.loads(row["requested_by"] or "[]"),
        status=row["status"],
        alternatives=json.loads(row["alternatives"] or "[]"),
        clarification_options=json.loads(row["clarification_options"] or "[]"),
        auto_picked=bool(row["auto_picked"]),
        product_id=row["product_id"],
        excluded=bool(row["excluded"]),
        note=row["note"] if "note" in row.keys() else None,
        created_at=row["created_at"],
    )


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.database_path
        self._schema_ready = False

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            if not self._schema_ready:
                await conn.executescript(SCHEMA)
                await conn.commit()
                self._schema_ready = True
            yield conn

    async def init(self) -> None:
        async with self._conn() as conn:
            try:
                await conn.execute("ALTER TABLE draft_items ADD COLUMN note TEXT")
                await conn.commit()
            except Exception:
                pass
            for stmt in (
                "ALTER TABLE order_history ADD COLUMN order_type TEXT DEFAULT 'instamart'",
                "ALTER TABLE order_history ADD COLUMN settlement_json TEXT",
                "ALTER TABLE order_history ADD COLUMN placed_by TEXT",
            ):
                try:
                    await conn.execute(stmt)
                    await conn.commit()
                except Exception:
                    pass
            await conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                ("household_id", settings.household_id),
            )
            await conn.commit()

    async def get_setting(self, key: str) -> str | None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ) as cur:
                row = await cur.fetchone()
                return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await conn.commit()

    async def list_settings_with_prefix(self, prefix: str) -> dict[str, str]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE ?",
                (f"{prefix}%",),
            ) as cur:
                rows = await cur.fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def add_message(
        self, household_id: str, sender: str, text: str, channel: str = "web"
    ) -> dict:
        async with self._conn() as conn:
            try:
                await conn.execute(
                    "ALTER TABLE messages ADD COLUMN channel TEXT DEFAULT 'web'"
                )
                await conn.commit()
            except Exception:
                pass
            await conn.execute(
                "INSERT INTO messages (household_id, sender, text, channel) VALUES (?, ?, ?, ?)",
                (household_id, sender, text, channel),
            )
            await conn.commit()
            async with conn.execute("SELECT last_insert_rowid()") as cur:
                row = await cur.fetchone()
                msg_id = row[0]
            async with conn.execute(
                "SELECT * FROM messages WHERE id = ?", (msg_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row)

    async def list_messages(self, household_id: str, limit: int = 100) -> list[dict]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT * FROM messages WHERE household_id = ? ORDER BY id DESC LIMIT ?",
                (household_id, limit),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in reversed(rows)]

    async def clear_messages(self, household_id: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "DELETE FROM messages WHERE household_id = ?", (household_id,)
            )
            await conn.commit()

    async def clear_draft(self, household_id: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "DELETE FROM draft_items WHERE household_id = ? AND status != 'checked_out'",
                (household_id,),
            )
            await conn.commit()

    async def list_draft_items(self, household_id: str) -> list[DraftItem]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT * FROM draft_items WHERE household_id = ? AND status != 'checked_out' ORDER BY id",
                (household_id,),
            ) as cur:
                rows = await cur.fetchall()
                return [_row_to_draft(r) for r in rows]

    async def get_draft_item(self, item_id: int) -> DraftItem | None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT * FROM draft_items WHERE id = ?", (item_id,)
            ) as cur:
                row = await cur.fetchone()
                return _row_to_draft(row) if row else None

    async def delete_draft_item(self, item_id: int, household_id: str) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute(
                """
                DELETE FROM draft_items
                WHERE id = ? AND household_id = ? AND status != 'checked_out'
                """,
                (item_id, household_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def insert_draft_item(
        self,
        household_id: str,
        raw_query: str,
        requested_by: list[str],
        status: DraftStatus,
        **kwargs: Any,
    ) -> DraftItem:
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO draft_items (
                  household_id, raw_query, resolved_sku, resolved_name, pack_size,
                  unit_price, merged_qty, requested_by, status, alternatives,
                  clarification_options, auto_picked, product_id, excluded, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    household_id,
                    raw_query,
                    kwargs.get("resolved_sku"),
                    kwargs.get("resolved_name"),
                    kwargs.get("pack_size"),
                    kwargs.get("unit_price"),
                    kwargs.get("merged_qty", 1),
                    json.dumps(requested_by),
                    status,
                    json.dumps(kwargs.get("alternatives", [])),
                    json.dumps(kwargs.get("clarification_options", [])),
                    int(kwargs.get("auto_picked", False)),
                    kwargs.get("product_id"),
                    int(kwargs.get("excluded", False)),
                    kwargs.get("note"),
                ),
            )
            await conn.commit()
            async with conn.execute("SELECT last_insert_rowid()") as cur:
                row = await cur.fetchone()
                item_id = row[0]
        item = await self.get_draft_item(item_id)
        assert item is not None
        return item

    async def update_draft_item(self, item_id: int, **kwargs: Any) -> DraftItem | None:
        fields: list[str] = []
        values: list[Any] = []
        json_fields = {"requested_by", "alternatives", "clarification_options"}
        bool_fields = {"auto_picked", "excluded"}

        for key, value in kwargs.items():
            if value is None and key not in (
                "resolved_sku",
                "resolved_name",
                "pack_size",
                "product_id",
                "unit_price",
            ):
                continue
            if key in json_fields:
                fields.append(f"{key} = ?")
                values.append(json.dumps(value))
            elif key in bool_fields:
                fields.append(f"{key} = ?")
                values.append(int(value))
            else:
                fields.append(f"{key} = ?")
                values.append(value)

        if not fields:
            return await self.get_draft_item(item_id)

        values.append(item_id)
        async with self._conn() as conn:
            await conn.execute(
                f"UPDATE draft_items SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            await conn.commit()
        return await self.get_draft_item(item_id)

    async def find_resolved_by_product(
        self, household_id: str, product_id: str
    ) -> DraftItem | None:
        async with self._conn() as conn:
            async with conn.execute(
                """
                SELECT * FROM draft_items
                WHERE household_id = ? AND product_id = ? AND status = 'resolved'
                ORDER BY id LIMIT 1
                """,
                (household_id, product_id),
            ) as cur:
                row = await cur.fetchone()
                return _row_to_draft(row) if row else None

    async def set_draft_excluded(self, item_id: int, excluded: bool) -> DraftItem | None:
        return await self.update_draft_item(item_id, excluded=excluded)

    async def mark_draft_checked_out(self, household_id: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE draft_items SET status = 'checked_out' WHERE household_id = ? AND status = 'resolved'",
                (household_id,),
            )
            await conn.commit()

    async def cache_catalog(
        self, raw_query: str, sku: str, name: str
    ) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO resolved_catalog_cache (raw_query, sku, name, last_checked)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(raw_query) DO UPDATE SET
                  sku = excluded.sku, name = excluded.name, last_checked = excluded.last_checked
                """,
                (raw_query.lower().strip(), sku, name, _now_iso()),
            )
            await conn.commit()

    async def list_recent_ordered_items(
        self, household_id: str, limit: int = 30
    ) -> list[dict]:
        async with self._conn() as conn:
            async with conn.execute(
                """
                SELECT items_json FROM order_history
                WHERE household_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (household_id, limit),
            ) as cur:
                rows = await cur.fetchall()

        items: list[dict] = []
        for row in rows:
            try:
                parsed = json.loads(row["items_json"] or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                items.append(
                    {
                        "name": item.get("resolved_name")
                        or item.get("name")
                        or item.get("raw_query"),
                        "spin_id": item.get("spinId")
                        or item.get("spin_id")
                        or item.get("sku"),
                        "product_id": item.get("product_id"),
                        "source": "local_history",
                    }
                )
        return items

    async def save_order_history(
        self,
        household_id: str,
        items_json: str,
        total: float,
        *,
        order_type: str = "instamart",
        settlement_json: str | None = None,
        placed_by: str | None = None,
    ) -> int:
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO order_history
                  (household_id, items_json, total, order_type, settlement_json, placed_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    household_id,
                    items_json,
                    total,
                    order_type,
                    settlement_json,
                    placed_by,
                ),
            )
            await conn.commit()
            async with conn.execute("SELECT last_insert_rowid()") as cur:
                row = await cur.fetchone()
                return int(row[0])

    async def list_order_history(
        self, household_id: str, limit: int = 30
    ) -> list[dict]:
        async with self._conn() as conn:
            async with conn.execute(
                """
                SELECT id, items_json, total, placed_at, order_type, settlement_json, placed_by
                FROM order_history
                WHERE household_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (household_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict] = []
        for row in rows:
            items: list = []
            settlement: list = []
            try:
                items = json.loads(row["items_json"] or "[]")
            except json.JSONDecodeError:
                pass
            try:
                settlement = json.loads(row["settlement_json"] or "[]")
            except json.JSONDecodeError:
                pass
            out.append(
                {
                    "id": row["id"],
                    "total": row["total"],
                    "placed_at": row["placed_at"],
                    "order_type": row["order_type"] or "instamart",
                    "placed_by": row["placed_by"],
                    "items": items,
                    "settlement": settlement,
                    "item_count": len(items) if isinstance(items, list) else 0,
                }
            )
        return out

    async def save_token(
        self, access_token: str, expires_at: str, client_id: str
    ) -> None:
        async with self._conn() as conn:
            await conn.execute("DELETE FROM oauth_tokens")
            await conn.execute(
                "INSERT INTO oauth_tokens (access_token, expires_at, client_id) VALUES (?, ?, ?)",
                (access_token, expires_at, client_id),
            )
            await conn.commit()

    async def get_token(self) -> dict | None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT * FROM oauth_tokens ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def clear_token(self) -> None:
        async with self._conn() as conn:
            await conn.execute("DELETE FROM oauth_tokens")
            await conn.commit()

    async def save_oauth_pending(
        self, state: str, code_verifier: str, client_id: str
    ) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO oauth_pending (state, code_verifier, client_id) VALUES (?, ?, ?)",
                (state, code_verifier, client_id),
            )
            await conn.commit()

    async def pop_oauth_pending(self, state: str) -> dict | None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT * FROM oauth_pending WHERE state = ?", (state,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                await conn.execute("DELETE FROM oauth_pending WHERE state = ?", (state,))
                await conn.commit()
                return dict(row)


db = Database()
