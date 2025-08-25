# app/linkwrap.py
from typing import Optional
from sqlalchemy import text
from app import db

def _get_cached(convo_id: int, raw_url: str) -> Optional[str]:
    with db.session() as s:
        row = s.execute(
            text("""
                select affiliate_url
                from links
                where conversation_id = :cid and raw_url = :u
                order by created_at desc
                limit 1
            """),
            {"cid": convo_id, "u": raw_url},
        ).first()
        return row[0] if row and row[0] else None

def wrap(raw_url: str, convo_id: Optional[int] = None) -> str:
    """
    Return an affiliate-looking link. For now we always return a safe fallback
    so the pipeline cannot error; we will wire Geniuslink/Skimlinks later.
    """
    if convo_id:
        cached = _get_cached(convo_id, raw_url)
        if cached:
            return cached

    # fallback affiliate format
    sep = '&' if '?' in raw_url else '?'
    return f"{raw_url}{sep}affid=bestie-test"
