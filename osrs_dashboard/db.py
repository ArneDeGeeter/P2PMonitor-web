import os
import sqlite3
from typing import Optional
from cryptography.fernet import Fernet
from .crypto import encrypt, decrypt
from .models import Account, Expense, SkillSnapshot, Snapshot

SKILLS = [
    "overall", "attack", "defence", "strength", "hitpoints", "ranged",
    "prayer", "magic", "cooking", "woodcutting", "fletching", "fishing",
    "firemaking", "crafting", "smithing", "mining", "herblore", "agility",
    "thieving", "slayer", "farming", "runecraft", "hunter", "construction",
    "sailing",
]

_SNAPSHOT_COLS = ", ".join(
    f"{s}_rank INTEGER, {s}_xp INTEGER" for s in SKILLS
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    login_email    BLOB,
    password       BLOB,
    bank_pin       BLOB,
    totp_secret    BLOB,
    proxy_url      BLOB,
    state          TEXT NOT NULL DEFAULT 'running',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS hiscore_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    polled_at   TEXT NOT NULL DEFAULT (datetime('now')),
    {_SNAPSHOT_COLS}
);
CREATE INDEX IF NOT EXISTS idx_snap_account_time
    ON hiscore_snapshots(account_id, polled_at DESC);

CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    type        TEXT NOT NULL DEFAULT 'expense',
    category    TEXT NOT NULL,
    currency    TEXT NOT NULL,
    amount      REAL NOT NULL,
    date_of     TEXT NOT NULL,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_account ON expenses(account_id, date_of DESC);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    acct_cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "state" not in acct_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN state TEXT NOT NULL DEFAULT 'running'")
    exp_cols = {r[1] for r in conn.execute("PRAGMA table_info(expenses)").fetchall()}
    if "type" not in exp_cols:
        conn.execute("ALTER TABLE expenses ADD COLUMN type TEXT NOT NULL DEFAULT 'expense'")
    conn.commit()


def open_db(path: str) -> sqlite3.Connection:
    path = os.path.expanduser(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def get_or_create_kdf_salt(conn: sqlite3.Connection) -> bytes:
    row = conn.execute("SELECT value FROM meta WHERE key='kdf_salt'").fetchone()
    if row:
        return bytes.fromhex(row["value"])
    salt = os.urandom(16)
    conn.execute("INSERT INTO meta(key,value) VALUES('kdf_salt',?)", (salt.hex(),))
    conn.commit()
    return salt


def _enc(f: Optional[Fernet], value: Optional[str]) -> Optional[bytes]:
    if value is None or value == "" or f is None:
        return None
    return encrypt(f, value)


def _dec(f: Fernet, value: Optional[bytes]) -> Optional[str]:
    if value is None:
        return None
    return decrypt(f, value)


VALID_STATES = ("running", "banned", "sold")


def insert_account(
    conn: sqlite3.Connection,
    fernet: Fernet,
    username: str,
    login_email: Optional[str] = None,
    password: Optional[str] = None,
    bank_pin: Optional[str] = None,
    totp_secret: Optional[str] = None,
    proxy_url: Optional[str] = None,
    state: str = "running",
    notes: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO accounts
           (username, login_email, password, bank_pin, totp_secret, proxy_url, state, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            username,
            _enc(fernet, login_email),
            _enc(fernet, password),
            _enc(fernet, bank_pin),
            _enc(fernet, totp_secret),
            _enc(fernet, proxy_url),
            state if state in VALID_STATES else "running",
            notes,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_account(
    conn: sqlite3.Connection,
    fernet: Fernet,
    account_id: int,
    username: str,
    login_email: Optional[str] = None,
    password: Optional[str] = None,
    bank_pin: Optional[str] = None,
    totp_secret: Optional[str] = None,
    proxy_url: Optional[str] = None,
    state: str = "running",
    notes: Optional[str] = None,
) -> None:
    conn.execute(
        """UPDATE accounts SET
           username=?, login_email=?, password=?, bank_pin=?,
           totp_secret=?, proxy_url=?, state=?, notes=?
           WHERE id=?""",
        (
            username,
            _enc(fernet, login_email),
            _enc(fernet, password),
            _enc(fernet, bank_pin),
            _enc(fernet, totp_secret),
            _enc(fernet, proxy_url),
            state if state in VALID_STATES else "running",
            notes,
            account_id,
        ),
    )
    conn.commit()


def set_account_state(conn: sqlite3.Connection, account_id: int, state: str) -> None:
    if state not in VALID_STATES:
        return
    conn.execute("UPDATE accounts SET state=? WHERE id=?", (state, account_id))
    conn.commit()


def delete_account(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute("""
        SELECT a.id, a.username, a.created_at, a.state, a.notes,
               s.polled_at AS last_polled,
               s.overall_xp,
               (SELECT COALESCE(SUM(e.amount),0) FROM expenses e
                WHERE e.account_id=a.id AND e.currency='GBP' AND e.type='expense') AS cost_gbp,
               (SELECT COALESCE(SUM(e.amount),0) FROM expenses e
                WHERE e.account_id=a.id AND e.currency='USD' AND e.type='expense') AS cost_usd,
               (SELECT COALESCE(SUM(e.amount),0) FROM expenses e
                WHERE e.account_id=a.id AND e.currency='EUR' AND e.type='expense') AS cost_eur,
               (SELECT COALESCE(SUM(e.amount),0) FROM expenses e
                WHERE e.account_id=a.id AND e.currency='GP' AND e.type='expense') AS cost_gp
        FROM accounts a
        LEFT JOIN hiscore_snapshots s ON s.id = (
            SELECT id FROM hiscore_snapshots
            WHERE account_id=a.id ORDER BY polled_at DESC LIMIT 1
        )
        ORDER BY a.username
    """).fetchall()

    accounts = []
    for r in rows:
        xp = r["overall_xp"] or 0
        accounts.append(Account(
            id=r["id"],
            username=r["username"],
            created_at=r["created_at"],
            state=r["state"] or "running",
            notes=r["notes"],
            last_polled=r["last_polled"],
            total_xp=xp,
            total_cost_gbp=r["cost_gbp"] or 0.0,
            total_cost_usd=r["cost_usd"] or 0.0,
            total_cost_eur=r["cost_eur"] or 0.0,
            total_cost_gp=r["cost_gp"] or 0.0,
        ))
    return accounts


def get_account(conn: sqlite3.Connection, account_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def get_account_secrets(conn: sqlite3.Connection, fernet: Fernet, account_id: int) -> dict:
    row = get_account(conn, account_id)
    if not row:
        return {}
    return {
        "username": row["username"],
        "login_email": _dec(fernet, row["login_email"]),
        "password": _dec(fernet, row["password"]),
        "bank_pin": _dec(fernet, row["bank_pin"]),
        "totp_secret": _dec(fernet, row["totp_secret"]),
        "proxy_url": _dec(fernet, row["proxy_url"]),
        "notes": row["notes"],
    }


def insert_snapshot(conn: sqlite3.Connection, account_id: int, csv_text: str) -> None:
    lines = [l.strip() for l in csv_text.strip().splitlines() if l.strip()]
    if len(lines) < len(SKILLS):
        return

    cols = []
    vals = []
    for i, skill in enumerate(SKILLS):
        parts = lines[i].split(",")
        if len(parts) < 3:
            rank, xp = -1, -1
        else:
            rank = int(parts[0]) if parts[0].strip() != "-1" else -1
            xp = int(parts[2]) if parts[2].strip() != "-1" else -1
        cols += [f"{skill}_rank", f"{skill}_xp"]
        vals += [rank, xp]

    placeholders = ", ".join("?" * len(vals))
    col_str = ", ".join(cols)
    conn.execute(
        f"INSERT INTO hiscore_snapshots (account_id, {col_str}) VALUES (?, {placeholders})",
        [account_id] + vals,
    )
    conn.commit()


def _row_to_snapshot(row: sqlite3.Row) -> dict:
    snap = {"polled_at": row["polled_at"], "skills": {}}
    for skill in SKILLS:
        snap["skills"][skill] = SkillSnapshot(
            rank=row[f"{skill}_rank"] or -1,
            level=_xp_to_level(row[f"{skill}_xp"] or 0) if skill != "overall" else 0,
            xp=row[f"{skill}_xp"] or 0,
        )
    if "overall" in snap["skills"]:
        total_xp = snap["skills"]["overall"].xp
        snap["skills"]["overall"] = SkillSnapshot(
            rank=snap["skills"]["overall"].rank,
            level=sum(
                _xp_to_level(snap["skills"][s].xp)
                for s in SKILLS if s != "overall"
            ),
            xp=total_xp,
        )
    return snap


def get_latest_snapshots(conn: sqlite3.Connection, account_id: int, limit: int = 2) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM hiscore_snapshots WHERE account_id=? ORDER BY polled_at DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()
    return [_row_to_snapshot(row) for row in rows]


def get_snapshot_at_or_before(conn: sqlite3.Connection, account_id: int, dt_str: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM hiscore_snapshots WHERE account_id=? AND polled_at <= ? ORDER BY polled_at DESC LIMIT 1",
        (account_id, dt_str),
    ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_earliest_snapshot(conn: sqlite3.Connection, account_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM hiscore_snapshots WHERE account_id=? ORDER BY polled_at ASC LIMIT 1",
        (account_id,),
    ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_all_accounts_snapshots_for_chart(
    conn: sqlite3.Connection,
    since_dt: str,
    until_dt: Optional[str] = None,
) -> list[dict]:
    """
    Returns an aggregated XP time series across all accounts.
    For each unique polled_at timestamp (within the range), sums the most recent
    known XP for every account per skill. Result is ordered by polled_at ASC.
    """
    account_ids = [r[0] for r in conn.execute("SELECT id FROM accounts").fetchall()]
    if not account_ids:
        return []

    # Per account: fetch baseline (last snapshot before since_dt) + all in range
    account_series: dict[int, list[tuple[str, dict[str, int]]]] = {}
    for acc_id in account_ids:
        rows = []
        baseline = conn.execute(
            "SELECT * FROM hiscore_snapshots WHERE account_id=? AND polled_at < ?"
            " ORDER BY polled_at DESC LIMIT 1",
            (acc_id, since_dt),
        ).fetchone()
        if baseline:
            rows.append(baseline)

        q = "SELECT * FROM hiscore_snapshots WHERE account_id=? AND polled_at >= ?"
        params: list = [acc_id, since_dt]
        if until_dt:
            q += " AND polled_at <= ?"
            params.append(until_dt)
        q += " ORDER BY polled_at ASC"
        rows.extend(conn.execute(q, params).fetchall())

        account_series[acc_id] = [
            (r["polled_at"], {s: r[f"{s}_xp"] or 0 for s in SKILLS})
            for r in rows
        ]

    # Collect unique timestamps within the requested range only
    timestamps = sorted({
        ts
        for snaps in account_series.values()
        for ts, _ in snaps
        if ts >= since_dt and (until_dt is None or ts <= until_dt)
    })
    if not timestamps:
        return []

    # Per-account baseline: first snapshot in our loaded data.
    # Using the first snapshot (which may be before since_dt) means a newly added account
    # contributes 0 gained XP at the moment it appears — only subsequent play counts.
    account_baselines: dict[int, dict[str, int]] = {
        acc_id: snaps[0][1] if snaps else {s: 0 for s in SKILLS}
        for acc_id, snaps in account_series.items()
    }

    # For each timestamp, carry-forward the latest known snapshot per account and sum
    result = []
    for ts in timestamps:
        totals: dict[str, int] = {s: 0 for s in SKILLS}
        gained: dict[str, int] = {s: 0 for s in SKILLS}
        for acc_id, snaps in account_series.items():
            last_skills = None
            for snap_ts, snap_skills in snaps:
                if snap_ts <= ts:
                    last_skills = snap_skills
                else:
                    break
            if last_skills:
                baseline = account_baselines[acc_id]
                for s in SKILLS:
                    totals[s] += last_skills[s]
                    gained[s] += max(0, last_skills[s] - baseline[s])
        result.append({"polled_at": ts, "skills": totals, "skills_gained": gained})
    return result


def get_all_snapshots_for_chart(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM hiscore_snapshots WHERE account_id=? ORDER BY polled_at ASC",
        (account_id,),
    ).fetchall()
    result = []
    for row in rows:
        entry = {"polled_at": row["polled_at"], "skills": {}}
        for skill in SKILLS:
            entry["skills"][skill] = row[f"{skill}_xp"] or 0
        result.append(entry)
    return result


def compute_deltas(prev: dict, curr: dict) -> dict:
    deltas = {}
    for skill in SKILLS:
        p = prev["skills"].get(skill)
        c = curr["skills"].get(skill)
        if p and c:
            delta = c.xp - p.xp
            deltas[skill] = delta if delta >= 0 else 0
    return deltas


def insert_expense(
    conn: sqlite3.Connection,
    account_id: int,
    category: str,
    currency: str,
    amount: float,
    date_of: str,
    type: str = "expense",
    notes: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO expenses (account_id,category,currency,amount,date_of,type,notes) VALUES (?,?,?,?,?,?,?)",
        (account_id, category, currency, amount, date_of, type if type in ("expense", "income") else "expense", notes),
    )
    conn.commit()
    return cur.lastrowid


def list_expenses(conn: sqlite3.Connection, account_id: int) -> list[Expense]:
    rows = conn.execute(
        "SELECT * FROM expenses WHERE account_id=? ORDER BY date_of DESC",
        (account_id,),
    ).fetchall()
    return [
        Expense(
            id=r["id"],
            account_id=r["account_id"],
            recorded_at=r["recorded_at"],
            category=r["category"],
            currency=r["currency"],
            amount=r["amount"],
            date_of=r["date_of"],
            type=r["type"] if r["type"] else "expense",
            notes=r["notes"],
        )
        for r in rows
    ]


def get_financial_overview(conn: sqlite3.Connection) -> dict:
    accounts = conn.execute(
        "SELECT id, username, state FROM accounts ORDER BY username"
    ).fetchall()

    currencies = ["GBP", "USD", "EUR", "GP"]
    global_totals = {c: {"spent": 0.0, "received": 0.0} for c in currencies}
    per_account = []

    for acc in accounts:
        entry = {
            "id": acc["id"],
            "username": acc["username"],
            "state": acc["state"] or "running",
            "by_currency": {},
        }
        for cur in currencies:
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE account_id=? AND currency=? AND type='expense'",
                (acc["id"], cur),
            ).fetchone()[0] or 0.0
            received = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE account_id=? AND currency=? AND type='income'",
                (acc["id"], cur),
            ).fetchone()[0] or 0.0
            entry["by_currency"][cur] = {"spent": spent, "received": received, "net": received - spent}
            global_totals[cur]["spent"] += spent
            global_totals[cur]["received"] += received
        per_account.append(entry)

    for cur in currencies:
        global_totals[cur]["net"] = global_totals[cur]["received"] - global_totals[cur]["spent"]

    return {"global": global_totals, "accounts": per_account, "currencies": currencies}


def delete_expense(conn: sqlite3.Connection, expense_id: int) -> None:
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()


_XP_TABLE = [0, 0, 83, 174, 276, 388, 512, 650, 801, 969, 1154,
             1358, 1584, 1833, 2107, 2411, 2746, 3115, 3523, 3973, 4470,
             5018, 5624, 6291, 7028, 7842, 8740, 9730, 10824, 12031, 13363,
             14833, 16456, 18247, 20224, 22406, 24815, 27473, 30408, 33648,
             37224, 41171, 45529, 50339, 55649, 61512, 67983, 75127, 83014,
             91721, 101333, 111945, 123660, 136594, 150872, 166636, 184040,
             203254, 224466, 247886, 273742, 302288, 333804, 368599, 407015,
             449428, 496254, 547953, 605032, 668051, 737627, 814445, 899257,
             992895, 1096278, 1210421, 1336443, 1475581, 1629200, 1798808,
             1986068, 2192818, 2421087, 2673114, 2951373, 3258594, 3597792,
             3972294, 4385776, 4842295, 5346332, 5902831, 6517253, 7195629,
             7944614, 8771558, 9684577, 10692629, 11805606, 13034431]


def _xp_to_level(xp: int) -> int:
    for level in range(98, 0, -1):
        if xp >= _XP_TABLE[level]:
            return level + 1
    return 1
