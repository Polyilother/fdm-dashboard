import json
import os
import sqlite3
import hashlib
import secrets
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_FILE = os.path.join(APP_DIR, "fdm_tasks.json")
DB_FILE = os.path.join(APP_DIR, "fdm_tasks.db")



PERMISSIONS = [
    "dispatch_task",
    "edit_device_status",
    "start_machine",
    "end_machine",
    "report_task_flow",
    "report_maintenance",
    "report_oee",
    "report_efficiency",
]


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def ensure_default_admin(conn):
    existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing:
        return
    conn.execute(
        "INSERT INTO users (username, password_hash, is_admin, active, permissions, created_at) VALUES (?, ?, 1, 1, ?, ?)",
        ("admin", hash_password("admin123"), json.dumps(PERMISSIONS, ensure_ascii=False), now_text()),
    )

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def normalize_task(task):
    task.setdefault("total_batches", 1)
    task.setdefault("gcode_names", [])
    task.setdefault("finished_batch_timestamps", [])
    task.setdefault("batch_statuses", ["待打印"] * task["total_batches"])
    task.setdefault("batch_start_times", ["-"] * task["total_batches"])
    task.setdefault("batch_end_times", ["-"] * task["total_batches"])
    task.setdefault("material", "未知")
    task.setdefault("special_notes", "无")
    task.setdefault("exception_log", "-")
    task.setdefault("transfer_notes", "-")
    task.setdefault("operator", "-")
    task.setdefault("end_operator", "-")
    task.setdefault("start_time", "-")
    task.setdefault("end_time", "-")
    task.setdefault("test_task_type", "未定义")
    task.setdefault("theory_total_hours", None)
    task.setdefault("eta_time", "-")
    task.setdefault("created_at", now_text())
    return task


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            permissions TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)


def main():
    if not os.path.exists(JSON_FILE):
        raise FileNotFoundError(f"找不到 JSON 文件: {JSON_FILE}")

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    with sqlite3.connect(DB_FILE) as conn:
        init_db(conn)
        ensure_default_admin(conn)
        existing = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if existing:
            print(f"SQLite 已有 {existing} 条任务，未重复导入。")
            print("默认管理员已确认：admin / admin123")
            return

        imported = 0
        for idx, task in enumerate(tasks):
            task = normalize_task(task)
            task_id = str(task.get("id") or f"legacy_{idx}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}")
            task["id"] = task_id
            conn.execute(
                "INSERT OR REPLACE INTO tasks (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (task_id, json.dumps(task, ensure_ascii=False), str(task.get("created_at", "")), now_text()),
            )
            imported += 1

    print(f"JSON 任务数: {len(tasks)}")
    print(f"导入 SQLite: {imported}")
    print("默认管理员已确认：admin / admin123")
    print(f"数据库文件: {DB_FILE}")


if __name__ == "__main__":
    main()
