from sqlalchemy import text
from sqlalchemy.orm import Session

def select_message_by_external_id(message_id: str):
    return text("select id from messages where message_id=:mid").bindparams(mid=message_id)

def get_or_create_user_by_phone(s: Session, phone: str):
    row = s.execute(text("select id from users where phone=:p"), {"p": phone}).first()
    if row:
        class U: pass
        u = U(); u.id = row[0]; return u
    new_id = s.execute(text("insert into users(phone) values(:p) returning id"), {"p": phone}).scalar()
    class U: pass
    u = U(); u.id = new_id; return u

def get_or_create_conversation(s: Session, user_id: int):
    row = s.execute(text("select id from conversations where user_id=:u order by started_at desc limit 1"), {"u": user_id}).first()
    if row:
        class C: pass
        c = C(); c.id = row[0]; return c
    new_id = s.execute(text("insert into conversations(user_id) values(:u) returning id"), {"u": user_id}).scalar()
    class C: pass
    c = C(); c.id = new_id; return c

def insert_message(s: Session, conversation_id: int, direction: str, message_id: str, textval: str):
    s.execute(
        text("insert into messages(conversation_id, direction, message_id, text) values(:c,:d,:m,:t)"),
        {"c": conversation_id, "d": direction, "m": message_id, "t": textval}
    )

def insert_click(s: Session, link_id: int, user_id: int):
    s.execute(text("insert into clicks(link_id, user_id) values(:l,:u)"), {"l": link_id, "u": user_id})

def insert_link(s: Session, conversation_id: int, raw_url: str, affiliate_url: str, campaign: str, commission_pct: float, sponsor_bid_cents: int):
    s.execute(
        text("""insert into links(conversation_id, raw_url, affiliate_url, campaign, commission_pct, sponsor_bid_cents)
                values(:c,:r,:a,:g,:p,:b)"""),
        {"c": conversation_id, "r": raw_url, "a": affiliate_url, "g": campaign, "p": commission_pct, "b": sponsor_bid_cents}
    )
