import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional


def _now_ts() -> int:
    return int(time.time())


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def parse_models_file(content: str) -> List[tuple[str, str]]:
    entries: List[tuple[str, str]] = []
    current_name = ""
    uuid_re = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        numbered = re.match(r"^\d+\.\s+(.+)$", line)
        if numbered:
            current_name = numbered.group(1).strip()
            continue

        if line.lower().startswith("id="):
            model_id = line.split("=", 1)[1].strip()
            if uuid_re.match(model_id):
                name = current_name or f"Model {model_id[:8]}"
                entries.append((name, model_id))

    return entries


class Store:
    def __init__(self, db_path: str = "data/app.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cookies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT UNIQUE NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_error TEXT DEFAULT '',
                    last_used_at INTEGER DEFAULT 0,
                    email TEXT DEFAULT '',
                    last_balance INTEGER DEFAULT 0,
                    last_checked_at INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    model_id TEXT UNIQUE NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS generation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider_generation_id TEXT,
                    used_cookie_id INTEGER,
                    model_id TEXT,
                    aspect_ratio TEXT,
                    prompt TEXT,
                    image_urls_json TEXT DEFAULT '[]',
                    saved_files_json TEXT DEFAULT '[]',
                    save_enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'success',
                    error_message TEXT DEFAULT '',
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_chat_state (
                    chat_id INTEGER PRIMARY KEY,
                    model_id TEXT DEFAULT '',
                    aspect_ratio TEXT DEFAULT '1:1',
                    updated_at INTEGER NOT NULL
                );
                """
            )
            self._ensure_cookie_columns(conn)
            conn.commit()

    def _ensure_cookie_columns(self, conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(cookies)").fetchall()}
        if "email" not in cols:
            conn.execute("ALTER TABLE cookies ADD COLUMN email TEXT DEFAULT ''")
        if "last_balance" not in cols:
            conn.execute("ALTER TABLE cookies ADD COLUMN last_balance INTEGER DEFAULT 0")
        if "last_checked_at" not in cols:
            conn.execute("ALTER TABLE cookies ADD COLUMN last_checked_at INTEGER DEFAULT 0")

    def bootstrap_defaults(self, model_file: str = "model_id.txt", cookie_file: Optional[str] = None) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM admin_users WHERE username = ?", ("admin",)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    ("admin", hash_password("admin123"), _now_ts()),
                )

            # Only seed defaults for missing keys; never overwrite admin-edited settings on restart.
            self._ensure_setting_conn(conn, "default_aspect_ratio", "1:1")
            self._ensure_setting_conn(conn, "auto_save_images", "0")
            self._ensure_setting_conn(conn, "save_images_dir", "data/generated")
            self._ensure_setting_conn(conn, "telegram_enabled", "0")
            self._ensure_setting_conn(conn, "telegram_bot_token", "")
            self._ensure_setting_conn(conn, "telegram_allowed_chat_ids", "")
            self._ensure_setting_conn(conn, "studio_enabled", "0")
            self._ensure_setting_conn(conn, "studio_proxy_base_url", "")
            self._ensure_setting_conn(conn, "studio_auth_username", "studio")
            self._ensure_setting_conn(conn, "studio_auth_password_hash", hash_password("studio123"))

            if os.path.exists(model_file):
                model_entries = parse_models_file(Path(model_file).read_text(encoding="utf-8"))
                for name, model_id in model_entries:
                    conn.execute(
                        "INSERT OR IGNORE INTO models (name, model_id, is_default, created_at) VALUES (?, ?, ?, ?)",
                        (name, model_id, 0, _now_ts()),
                    )

                # Clean up old invalid rows from legacy parser behavior.
                conn.execute(
                    "DELETE FROM models WHERE model_id NOT GLOB '????????-????-????-????-????????????'"
                )

            has_default = conn.execute("SELECT id FROM models WHERE is_default = 1").fetchone()
            if has_default is None:
                first_model = conn.execute("SELECT id FROM models ORDER BY id ASC LIMIT 1").fetchone()
                if first_model is not None:
                    conn.execute("UPDATE models SET is_default = 1 WHERE id = ?", (first_model["id"],))

            if cookie_file and os.path.exists(cookie_file):
                text = Path(cookie_file).read_text(encoding="utf-8")
                for raw in text.splitlines():
                    cookie = raw.strip()
                    if not cookie:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO cookies (value, is_active, created_at) VALUES (?, 1, ?)",
                        (cookie, _now_ts()),
                    )

            conn.commit()

    def verify_admin(self, username: str, password: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM admin_users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                return False
            return row["password_hash"] == hash_password(password)

    def list_cookies(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cookies ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def list_active_cookies(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cookies WHERE is_active = 1 ORDER BY last_used_at ASC, id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_cookie(self, cookie_value: str) -> None:
        cookie_value = cookie_value.strip()
        if not cookie_value:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cookies (value, is_active, created_at) VALUES (?, 1, ?)",
                (cookie_value, _now_ts()),
            )
            conn.commit()

    def get_cookie_by_value(self, cookie_value: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cookies WHERE value = ? LIMIT 1", (cookie_value,)).fetchone()
            return None if row is None else dict(row)

    def update_cookie_value(self, cookie_id: int, cookie_value: str) -> bool:
        cookie_value = (cookie_value or "").strip()
        if not cookie_value:
            return False
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM cookies WHERE id = ? LIMIT 1", (cookie_id,)).fetchone()
            if row is None:
                return False
            if (row["value"] or "").strip() == cookie_value:
                return False

            try:
                conn.execute("UPDATE cookies SET value = ? WHERE id = ?", (cookie_value, cookie_id))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Keep old value if another row already stores this exact auth payload.
                return False

    def delete_cookie(self, cookie_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM cookies WHERE id = ?", (cookie_id,))
            conn.commit()

    def toggle_cookie(self, cookie_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE cookies SET is_active = ? WHERE id = ?", (1 if enabled else 0, cookie_id))
            conn.commit()

    def mark_cookie_used(self, cookie_id: int) -> None:
        with self._connect() as conn:
            now = _now_ts()
            conn.execute(
                "UPDATE cookies SET last_used_at = ?, last_checked_at = ?, last_error = '' WHERE id = ?",
                (now, now, cookie_id),
            )
            conn.commit()

    def mark_cookie_error(self, cookie_id: int, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE cookies SET last_error = ?, last_checked_at = ? WHERE id = ?",
                (message[:300], _now_ts(), cookie_id),
            )
            conn.commit()

    def update_cookie_profile(self, cookie_id: int, email: str, balance: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE cookies SET email = ?, last_balance = ?, last_checked_at = ? WHERE id = ?",
                (email[:200], int(balance), _now_ts(), cookie_id),
            )
            conn.commit()

    def cookie_health_summary(self) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT is_active, last_balance FROM cookies").fetchall()
            active_nonzero = 0
            active_zero = 0
            for row in rows:
                if int(row["is_active"] or 0) != 1:
                    continue
                if int(row["last_balance"] or 0) > 0:
                    active_nonzero += 1
                else:
                    active_zero += 1
            return {
                "active_nonzero": active_nonzero,
                "active_zero": active_zero,
            }

    def list_models(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM models ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def add_model(self, name: str, model_id: str) -> None:
        name = name.strip() or f"Model {model_id[:8]}"
        model_id = model_id.strip()
        if not model_id:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO models (name, model_id, is_default, created_at) VALUES (?, ?, 0, ?)",
                (name, model_id, _now_ts()),
            )
            conn.commit()

    def delete_model(self, model_db_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM models WHERE id = ?", (model_db_id,))
            has_default = conn.execute("SELECT id FROM models WHERE is_default = 1").fetchone()
            if has_default is None:
                first_model = conn.execute("SELECT id FROM models ORDER BY id ASC LIMIT 1").fetchone()
                if first_model is not None:
                    conn.execute("UPDATE models SET is_default = 1 WHERE id = ?", (first_model["id"],))
            conn.commit()

    def set_default_model(self, model_db_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE models SET is_default = 0")
            conn.execute("UPDATE models SET is_default = 1 WHERE id = ?", (model_db_id,))
            conn.commit()

    def get_default_model_id(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT model_id FROM models WHERE is_default = 1 LIMIT 1").fetchone()
            return None if row is None else row["model_id"]

    def get_model_by_model_id(self, model_id: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM models WHERE model_id = ? LIMIT 1", (model_id,)).fetchone()
            return None if row is None else dict(row)

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return default if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            self._set_setting_conn(conn, key, value)
            conn.commit()

    def _set_setting_conn(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _ensure_setting_conn(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    def stats(self) -> Dict[str, int]:
        with self._connect() as conn:
            total_cookies = conn.execute("SELECT COUNT(*) AS c FROM cookies").fetchone()["c"]
            active_cookies = conn.execute("SELECT COUNT(*) AS c FROM cookies WHERE is_active = 1").fetchone()["c"]
            total_models = conn.execute("SELECT COUNT(*) AS c FROM models").fetchone()["c"]
            return {
                "total_cookies": total_cookies,
                "active_cookies": active_cookies,
                "total_models": total_models,
            }

    def add_generation_log(
        self,
        provider_generation_id: str,
        used_cookie_id: int,
        model_id: str,
        aspect_ratio: str,
        prompt: str,
        image_urls_json: str,
        saved_files_json: str,
        save_enabled: bool,
        status: str,
        error_message: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO generation_logs (
                    provider_generation_id,
                    used_cookie_id,
                    model_id,
                    aspect_ratio,
                    prompt,
                    image_urls_json,
                    saved_files_json,
                    save_enabled,
                    status,
                    error_message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_generation_id,
                    used_cookie_id,
                    model_id,
                    aspect_ratio,
                    prompt,
                    image_urls_json,
                    saved_files_json,
                    1 if save_enabled else 0,
                    status,
                    (error_message or "")[:400],
                    _now_ts(),
                ),
            )
            conn.commit()

    def list_generation_logs(self, limit: int = 50) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generation_logs ORDER BY id DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_telegram_chat_state(self, chat_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id, model_id, aspect_ratio, updated_at FROM telegram_chat_state WHERE chat_id = ? LIMIT 1",
                (int(chat_id),),
            ).fetchone()
            return None if row is None else dict(row)

    def upsert_telegram_chat_state(self, chat_id: int, model_id: str, aspect_ratio: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_chat_state (chat_id, model_id, aspect_ratio, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    model_id = excluded.model_id,
                    aspect_ratio = excluded.aspect_ratio,
                    updated_at = excluded.updated_at
                """,
                (
                    int(chat_id),
                    (model_id or "").strip(),
                    (aspect_ratio or "1:1").strip() or "1:1",
                    _now_ts(),
                ),
            )
            conn.commit()
