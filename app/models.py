# app/models.py
from __future__ import annotations

import re
import json
from typing import Optional, List, Dict

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

# ------------------------- helpers ------------------------- #

def _col_exists(s: Session, table: str, col: str) -> bool:
    try:
        r = s.execute(sqltext(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name = :t AND column_name = :c
             LIMIT 1
            """
        ), {"t": table, "c": col}).first()
        return bool(r)
    except Exception:
        return False

def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits

# Consistent message dict for AI memory, UI, etc.
def _row_to_message_dict(r, has_phone: bool, has_meta: bool) -> Dict:
    d = {
        "id": r[0],
        "conversation_id": r[1],
        "direction": r[2],
        "role": "user" if (r[2] == "in") else "assistant",
        "message_id": r[3],
        "text": r[4] or "",
        "created_at": r[5],
    }
    idx = 6
    if has_phone:
        d["phone"] = r[idx]; idx += 1
    if has_meta:
        d["meta"] = r[idx]; idx += 1
    return d

# ------------------------- queries ------------------------- #

def select_message_by_external_id(message_id: str):
    """Back-compat helper used in some flows."""
    return sqltext("select id from messages where message_id=:mid").bindparams(mid=message_id)

# ------------------------- users --------------------------- #

def get_or_create_user_by_phone(s: Session, phone: str):
    """
    Idempotent user fetch/create by phone.
    - Normalizes to E.164 (US default).
    - If an existing row has unnormalized phone, we still match and update it.
    Returns dot-object with .id for backward compatibility.
    """
    norm = _normalize_phone(phone) or phone
    row = s.execute(sqltext("select id from users where phone=:p"), {"p": norm}).first()
    if not row:
        row = s.execute(sqltext("select id from users where phone=:p"), {"p": phone}).first()

    if row:
        uid = row[0]
        try:
            s.execute(sqltext("update users set phone=:p where id=:u and phone<>:p"),
                      {"p": norm, "u": uid})
            s.flush()
        except Exception:
            pass
        class U: pass
        u = U(); u.id = uid; return u

    new_id = s.execute(
        sqltext("insert into users(phone) values(:p) returning id"),
        {"p": norm}
    ).scalar()

    class U: pass
    u = U(); u.id = new_id; return u

# ---------------------- conversations ---------------------- #

def get_or_create_conversation(s: Session, user_id: int):
    """
    Return most-recent conversation for user or create one.
    Prefers started_at, then created_at, else falls back to highest id.
    """
    order_col = "started_at" if _col_exists(s, "conversations", "started_at") else (
        "created_at" if _col_exists(s, "conversations", "created_at") else "id"
    )
    row = s.execute(sqltext(
        f"select id from conversations where user_id=:u order by {order_col} desc limit 1"
    ), {"u": user_id}).first()

    if row:
        class C: pass
        c = C(); c.id = row[0]; return c

    new_id = s.execute(
        sqltext("insert into conversations(user_id) values(:u) returning id"),
        {"u": user_id}
    ).scalar()

    class C: pass
    c = C(); c.id = new_id; return c

def save_thread_title_if_empty(s: Session, conversation_id: int, title: str):
    """Set conversations.title if the column exists and it's currently NULL/empty."""
    if not _col_exists(s, "conversations", "title"):
        return
    s.execute(sqltext("""
        UPDATE conversations
           SET title = :t
         WHERE id = :cid AND (title IS NULL OR title = '')
    """), {"t": title.strip()[:120], "cid": conversation_id})

def update_conversation_title(s: Session, conversation_id: int, title: str):
    """Force-set conversations.title if the column exists."""
    if not _col_exists(s, "conversations", "title"):
        return
    s.execute(sqltext("UPDATE conversations SET title=:t WHERE id=:cid"),
              {"t": title.strip()[:160], "cid": conversation_id})

# ------------------------- messages ------------------------ #

def message_exists(s: Session, message_id: str) -> bool:
    r = s.execute(sqltext("select 1 from messages where message_id=:m limit 1"), {"m": message_id}).first()
    return bool(r)

def insert_message(
    s: Session,
    conversation_id: int,
    direction: str,
    message_id: str,
    textval: str,
    *,
    phone: Optional[str] = None,
    meta: Optional[dict] = None,
):
    """
    Insert a message with duplicate safety and optional phone/meta columns.
    - direction: 'in' or 'out'
    - message_id: external or generated id (we avoid duplicates)
    - phone: stored if messages.phone exists
    - meta: dict stored as JSON if messages.meta exists
    """
    if message_exists(s, message_id):
        return

    has_phone = _col_exists(s, "messages", "phone")
    has_meta  = _col_exists(s, "messages", "meta")
    cols = ["conversation_id", "direction", "message_id", "text"]
    vals = { "c": conversation_id, "d": direction, "m": message_id, "t": textval }

    if has_phone:
        cols.append("phone")
        vals["p"] = _normalize_phone(phone) if phone else None
    if has_meta:
        cols.append("meta")
        vals["j"] = json.dumps(meta) if isinstance(meta, dict) else (meta if meta else None)

    placeholders = []
    param_map = {}
    for col in cols:
        key = {"conversation_id": "c", "direction": "d", "message_id": "m", "text": "t",
               "phone": "p", "meta": "j"}[col]
        placeholders.append(f":{key}")
        param_map[key] = vals.get(key)

    sql = f"insert into messages({', '.join(cols)}) values({', '.join(placeholders)})"
    try:
        sql += " on conflict (message_id) do nothing"
    except Exception:
        pass

    s.execute(sqltext(sql), param_map)

def get_recent_messages_for_conversation(
    s: Session,
    conversation_id: int,
    limit: int = 20
) -> List[Dict]:
    """Return last N messages (both directions) newest→oldest as a list of dicts."""
    has_phone = _col_exists(s, "messages", "phone")
    has_meta  = _col_exists(s, "messages", "meta")
    extra = (", phone" if has_phone else "") + (", meta" if has_meta else "")
    rows = s.execute(sqltext(f"""
        SELECT id, conversation_id, direction, message_id, text, created_at{extra}
          FROM messages
         WHERE conversation_id=:cid
      ORDER BY created_at DESC
         LIMIT :lim
    """), {"cid": conversation_id, "lim": limit}).fetchall()
    return [_row_to_message_dict(r, has_phone, has_meta) for r in rows]

def get_recent_turns_for_user(
    s: Session,
    user_id: int,
    limit: int = 20
) -> List[Dict]:
    """
    Return last N messages across the user's conversations newest→oldest.
    Useful for fallbacks (seed memory if Redis cold).
    """
    has_phone = _col_exists(s, "messages", "phone")
    has_meta  = _col_exists(s, "messages", "meta")
    extra = (", m.phone" if has_phone else "") + (", m.meta" if has_meta else "")
    rows = s.execute(sqltext(f"""
        SELECT m.id, m.conversation_id, m.direction, m.message_id, m.text, m.created_at{extra}
          FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
         WHERE c.user_id = :u
      ORDER BY m.created_at DESC
         LIMIT :lim
    """), {"u": user_id, "lim": limit}).fetchall()
    return [_row_to_message_dict(r, has_phone, has_meta) for r in rows]

def load_recent_turns_for_ai(
    s: Session,
    conversation_id: int,
    limit: int = 12
) -> List[Dict]:
    """
    Returns a ready-to-use list of {role, content} for AI calls, oldest→newest,
    trimmed to the last `limit`.
    """
    msgs = get_recent_messages_for_conversation(s, conversation_id, limit=limit*2)
    msgs = list(reversed(msgs))[:limit]
    out: List[Dict] = []
    for m in msgs:
        out.append({"role": m["role"], "content": m["text"]})
    return out

def search_user_messages(
    s: Session,
    user_id: int,
    query: str,
    limit: int = 50
) -> List[Dict]:
    """
    Simple full-text-ish search over messages.text for a user.
    Returns newest→oldest subset.
    """
    q = f"%{(query or '').strip()}%"
    has_phone = _col_exists(s, "messages", "phone")
    has_meta  = _col_exists(s, "messages", "meta")
    extra = (", m.phone" if has_phone else "") + (", m.meta" if has_meta else "")
    rows = s.execute(sqltext(f"""
        SELECT m.id, m.conversation_id, m.direction, m.message_id, m.text, m.created_at{extra}
          FROM messages m
          JOIN conversations c ON c.id = m.conversation_id
         WHERE c.user_id=:u AND m.text ILIKE :q
      ORDER BY m.created_at DESC
         LIMIT :lim
    """), {"u": user_id, "q": q, "lim": limit}).fetchall()
    return [_row_to_message_dict(r, has_phone, has_meta) for r in rows]

# -------------------- links & clicks ----------------------- #

def insert_click(s: Session, link_id: int, user_id: int):
    s.execute(sqltext("insert into clicks(link_id, user_id) values(:l,:u)"),
              {"l": link_id, "u": user_id})

def insert_link(
    s: Session,
    conversation_id: int,
    raw_url: str,
    affiliate_url: str,
    campaign: str,
    commission_pct: float,
    sponsor_bid_cents: int
):
    s.execute(
        sqltext(
            """
            insert into links(conversation_id, raw_url, affiliate_url, campaign, commission_pct, sponsor_bid_cents)
            values(:c,:r,:a,:g,:p,:b)
            """
        ),
        {"c": conversation_id, "r": raw_url, "a": affiliate_url, "g": campaign, "p": commission_pct, "b": sponsor_bid_cents}
    )

# -------------------- user profile utils ------------------- #

def ensure_user_profile_row(s: Session, user_id: int):
    """
    Ensure there's a row in user_profiles so plan gates won't 500 on new users.
    No-op if row exists.
    """
    r = s.execute(sqltext("select 1 from public.user_profiles where user_id=:u limit 1"), {"u": user_id}).first()
    if r:
        return
    s.execute(sqltext(
        """
        insert into public.user_profiles(user_id, plan_status, daily_counter_date, daily_msgs_used, is_quiz_completed)
        values(:u, 'pending', CURRENT_DATE, 0, false)
        """
    ), {"u": user_id})

def get_user_profile_snapshot(s: Session, user_id: int) -> Dict:
    """
    Returns a dict of persona/basics from user_profiles (only keys that exist).
    """
    cols = ["persona", "bestie_name", "sizes", "brands", "budget_range", "sensitivities", "memory_notes",
            "plan_status", "trial_start_date", "plan_renews_at", "is_quiz_completed"]
    select_cols = []
    for c in cols:
        if _col_exists(s, "user_profiles", c):
            select_cols.append(c)
    if not select_cols:
        return {}
    row = s.execute(sqltext(f"""
        SELECT {', '.join(select_cols)} FROM public.user_profiles WHERE user_id=:u
    """), {"u": user_id}).first()
    if not row:
        return {}
    snap = {}
    for i, c in enumerate(select_cols):
        snap[c] = row[i]
    return snap

def append_memory_note(s: Session, user_id: int, note: str):
    """
    Append a short note to user_profiles.memory_notes if the column exists.
    """
    if not _col_exists(s, "user_profiles", "memory_notes"):
        return
    s.execute(sqltext("""
        UPDATE public.user_profiles
           SET memory_notes = CONCAT(COALESCE(memory_notes,''), CASE WHEN memory_notes IS NULL OR memory_notes='' THEN '' ELSE '\n' END, :n)
         WHERE user_id = :u
    """), {"n": note.strip()[:1000], "u": user_id})

def set_quiz_completed(s: Session, user_id: int, completed: bool = True):
    if _col_exists(s, "user_profiles", "is_quiz_completed"):
        s.execute(sqltext("UPDATE public.user_profiles SET is_quiz_completed=:v WHERE user_id=:u"),
                  {"v": completed, "u": user_id})

def upsert_user_persona(s: Session, user_id: int, *, persona_addon: Optional[str] = None, bestie_name: Optional[str] = None):
    """
    Update persona fields that exist. Pass only what you want changed.
    """
    sets = []
    params = {"u": user_id}
    if persona_addon is not None and _col_exists(s, "user_profiles", "persona"):
        sets.append("persona=:persona"); params["persona"] = persona_addon
    if bestie_name is not None and _col_exists(s, "user_profiles", "bestie_name"):
        sets.append("bestie_name=:bn"); params["bn"] = bestie_name
    if not sets:
        return
    s.execute(sqltext(f"UPDATE public.user_profiles SET {', '.join(sets)} WHERE user_id=:u"), params)

# -------------------- meta update helper ------------------- #

def update_message_meta_field(s: Session, message_id: str, key: str, value):
    """
    Safely update messages.meta JSON field with a single key/value (if column exists).
    """
    if not _col_exists(s, "messages", "meta"):
        return
    row = s.execute(sqltext("SELECT meta FROM messages WHERE message_id=:m"), {"m": message_id}).first()
    meta = {}
    if row and row[0]:
        try:
            meta = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception:
            meta = {}
    meta[key] = value
    s.execute(sqltext("UPDATE messages SET meta=:j WHERE message_id=:m"),
              {"j": json.dumps(meta), "m": message_id})
