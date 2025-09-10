# app/ai.py
"""
Bestie AI brain — persona compose, memory, multimodal, product recs, tone QA.

What you get:
- compose_persona(user_id, session_goal): pulls Bestie Core + quiz + name
- build_messages(...): persona + last 8–12 turns from Redis + current ask
- generate_reply(...): single entrypoint used by workers.py
- rewrite_if_cringe / rewrite_different: tone rescue utilities
- describe_image / transcribe_and_respond: multimodal routes
- health_check(user_id): sanity snapshot for QA pipelines

Notes:
- No em dashes in user-facing output. We sanitize.
- Links are left intact for workers.py to run link hygiene.
"""

from __future__ import annotations

import os
import re
import json
import random
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone
from app import bestie_qc

import redis
from loguru import logger
from openai import OpenAI
from sqlalchemy import text as sqltext
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from app.bestie_oneliners import render_oneliner_with_link

from app import db, linkwrap
from app.personas.bestie_altare import BESTIE_SYSTEM_PROMPT
from typing import Optional, Dict

# ------------------ OpenAI client ------------------ #
CLIENT: Optional[OpenAI] = None
try:
    CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    logger.info("[AI] OpenAI client initialized")
except Exception as e:
    logger.error("[AI] OpenAI init failed: {}", e)

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ------------------ Redis memory ------------------- #
REDIS_URL = os.getenv("REDIS_URL", "")
_rds: Optional[redis.Redis] = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
HIST_KEY = "bestie:history:{user_id}"        # list of json messages
HIST_MAX = 24                                # keep up to 24, send last 8–12 to GPT

# ------------------ Product/pack links ------------- #
PROMPT_PACK_LINKS = {
    "Confidence Cleanse": "https://240026861589.gumroad.com/l/ymrag",
    "Unhinged But Healed": "https://240026861589.gumroad.com/l/gxqrl",
    "Breaking Patterns": "https://240026861589.gumroad.com/l/umqgme",
    "Messy Success": "https://240026861589.gumroad.com/l/gifai",
    "Clarity Unlocked": "https://240026861589.gumroad.com/l/yqyqbd",
    "Emotional Resilience": "https://240026861589.gumroad.com/l/askqyr",
    "Self-Love Revolution": "https://240026861589.gumroad.com/l/rwerok",
    "Goal Crusher": "https://240026861589.gumroad.com/l/uabyv",
    "Release & Rise": "https://240026861589.gumroad.com/l/begcoi",
    "Social Confidence": "https://240026861589.gumroad.com/l/gdgef",
    "Emotional Freedom": "https://240026861589.gumroad.com/l/oeyuyc",
    "Purpose Unlocked": "https://240026861589.gumroad.com/l/dhjlki",
    "Vulnerable Power": "https://240026861589.gumroad.com/l/bgsyw",
    "Creative Unleashed": "https://240026861589.gumroad.com/l/ueampy",
    "Mental Clarity": "https://240026861589.gumroad.com/l/ggcff",
    "Glow-Up & Show-Up": "https://240026861589.gumroad.com/l/huxjpq",
    "Momming Like a Boss": "https://240026861589.gumroad.com/l/urbcm",
    "Stacking Cash and Kicking Ass": "https://240026861589.gumroad.com/l/lkxeqsu",
    "Get Rich or Cry Trying": "https://240026861589.gumroad.com/l/hyvinw",
    "Zen as F*ck": "https://240026861589.gumroad.com/l/jspag",
    "Astrology Bitch, You’re a Star": "https://240026861589.gumroad.com/l/zgwbz",
    "Spiritual Gangster": "https://240026861589.gumroad.com/l/evpkn",
    "Romantic Rebel": "https://240026861589.gumroad.com/l/gcjhu",
    "I'm Not Crying, You're Crying": "https://240026861589.gumroad.com/l/szgzl",
    "Unapologetically Me": "https://240026861589.gumroad.com/l/cmajvu",
    "Relationship Audit": "https://240026861589.gumroad.com/l/eeauan",
    "Dealing with Your Asshole Boss": "https://240026861589.gumroad.com/l/qymwa",
    "New Moon, New You": "https://240026861589.gumroad.com/l/morsi",
    "Sleep Trainer": "https://240026861589.gumroad.com/l/eaezro",
    "Text Analysis": "https://240026861589.gumroad.com/l/nmfva",
    "Productivity Powerhouse": "https://240026861589.gumroad.com/l/weuewc",
    "Get That Bag Bitch": "https://240026861589.gumroad.com/l/eqaegl",
    "Mindful Morning Goddess": "https://240026861589.gumroad.com/l/tqxmqb",
    "Break Up with Bad Habits": "https://240026861589.gumroad.com/l/plscw",
    "Fix the Love, Not the Drama": "https://240026861589.gumroad.com/l/fkohe",
    "Healthy Habits, Happy Life": "https://240026861589.gumroad.com/l/rxajl",
    "From Dreamer to CEO": "https://240026861589.gumroad.com/l/zdcddh",
    "Daddy Issues? Meet Your Inner Queen": "https://240026861589.gumroad.com/l/dsjez",
    "To Buy or Not to Buy: The Car Edition": "https://240026861589.gumroad.com/l/glibzy",
    "To Buy or Not to Buy: The Home Edition": "https://240026861589.gumroad.com/l/udjdw",
    "Overcoming Perfectionism": "https://240026861589.gumroad.com/l/expgkn",
    "Surviving a Toxic Work Environment": "https://240026861589.gumroad.com/l/vxcoq",
    "Navigating a Major Life Transition": "https://240026861589.gumroad.com/l/clftz",
    "Self-Care Like a Queen": "https://240026861589.gumroad.com/l/gleer",
    "Start Your Fitness Journey": "https://240026861589.gumroad.com/l/ybqrt",
    "Am I Ready to Pull the Trigger?": "https://240026861589.gumroad.com/l/iithu",
}

# ------------------ Tone banks --------------------- #
BANNED_STOCK_PHRASES = [
    # high cringe
    "vacation in a bottle",
    "spa day in your pocket",
    "sun-kissed glow",
    "your skin will thank you",
    "beauty arsenal",
    "say goodbye to",
    "hello to",
    "main character in every room",
    "begging for a glow-up",
    "strutting like you just stepped off a yacht",
    "daily adventures",
    "as an ai",
    "i am just a",
    "you are not alone",
    "i understand you are feeling",
    "beyoncé has flop days",
    "you’re still headlining",
]
PRODUCT_ONE_LINERS = [
    "Looks like designer without the regret.",
    "High-end finish, low-effort energy.",
    "Quiet flex everyone asks you about.",
    "Polished without trying too hard.",
    "Expensive look, sensible receipt.",
]

OPENING_BANNED = [
    "it sounds like", "i understand that", "you're not alone",
    "i'm sorry you're", "technology can be", "i get that"
]

# ------------------ Helpers ------------------------ #
def _sanitize_output(text: str) -> str:
    """Keep house style: no em dashes, trim, single spaces."""
    if not text:
        return text
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

def _sentiment_hint(user_text: str) -> str:
    t = (user_text or "").lower()
    if any(x in t for x in ["angry", "annoyed", "pissed", "frustrated", "wtf", "broken", "hate this"]):
        return "User is frustrated. Be decisive and brief. Skip therapy cliches."
    if any(x in t for x in ["excited", "love", "omg", "obsessed"]):
        return "User is excited. Match energy and lean into enthusiasm."
    return ""
def single_line(system: str, user: str, *, max_tokens: int = 60, temperature: float = 0.7) -> str:
    """
    Ask the model for exactly one short line.
    Uses the global CLIENT (initialized above). Returns "" on any failure.
    """
    try:
        # If no client/API key, skip gracefully
        if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
            return ""

        resp = CLIENT.chat.completions.create(
            model=OPENAI_MODEL,   # already defined at top of this file
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            n=1,
        )
        text = (resp.choices[0].message.content or "").strip()
        # force single line
        return text.splitlines()[0].strip()
    except Exception as e:
        try:
            logger.warning("[AI] single_line failed: {}", e)
        except Exception:
            pass
        return ""

# ------------------------------------------------------------------------
# Multimodal helpers (allow [IMG:url] inline tags if workers ever pass them through)
_IMG_TAG = re.compile(r"\[IMG:([^\]]+)\]")

def _extract_img_urls(text: str) -> Tuple[str, List[str]]:
    urls: list[str] = []
    def _cap(m):
        urls.append(m.group(1).strip())
        return ""
    clean = _IMG_TAG.sub(_cap, text or "")
    return clean.strip(), urls

def _to_mm_user_content(text: str) -> List[Dict]:
    txt, urls = _extract_img_urls(text)
    if not urls:
        return [{"type": "text", "text": txt}]
    parts: List[Dict] = []
    if txt:
        parts.append({"type": "text", "text": txt})
    for u in urls:
        parts.append({"type": "image_url", "image_url": {"url": u}})
    return parts

# ------------------ Persona compose ---------------- #
DEFAULT_NAME = "Bestie"

def _fetch_profile_bits(user_id: Optional[int]) -> Tuple[str, str, str]:
    """
    Return (persona_addon, bestie_name, quiz_profile_text)
    """
    if not user_id:
        return "", DEFAULT_NAME, ""
    try:
        with db.session() as s:
            row = s.execute(
                sqltext("""
                    SELECT
                      COALESCE(persona,'') AS persona,
                      COALESCE(bestie_name,'') AS bestie_name,
                      COALESCE(sizes::text,'') AS sizes,
                      COALESCE(brands::text,'') AS brands,
                      COALESCE(budget_range,'') AS budget,
                      COALESCE(sensitivities::text,'') AS sensitivities,
                      COALESCE(memory_notes,'') AS memory_notes
                    FROM user_profiles
                    WHERE user_id = :uid
                """),
                {"uid": user_id}
            ).first()

        persona_addon = (row[0] or "").strip() if row else ""
        bestie_name = (row[1] or "").strip() if row else DEFAULT_NAME

        quiz_data = {
            "Sizes": row[2] if row else "",
            "Favorite Brands": row[3] if row else "",
            "Budget Range": row[4] if row else "",
            "Topics to Avoid": row[5] if row else "",
            "Emotional Notes": row[6] if row else "",
        }
        quiz_profile_lines = [f"{k}: {v}" for k, v in quiz_data.items() if v]
        quiz_profile_text = "\n".join(quiz_profile_lines)
        return persona_addon, (bestie_name or DEFAULT_NAME), quiz_profile_text
    except Exception as e:
        logger.exception("[AI][Persona] Fetch failed for user_id={}: {}", user_id, e)
        return "", DEFAULT_NAME, ""

def compose_persona(user_id: Optional[int], session_goal: Optional[str] = None) -> str:
    """
    Build the master system prompt: Bestie Core + banned list + quiz + optional goal.
    """
    addon, bestie_name, quiz_profile = _fetch_profile_bits(user_id)

    core = BESTIE_SYSTEM_PROMPT or ""
    if not core.strip():
        # Fallback persona if file missing
        core = (
            "You are Schizo Bestie — a stylish, emotionally fluent, pop-culture–savvy best friend. "
            "Be concise, helpful, witty, and precise. No therapy cliches. No robotic filler."
        )

    policy = f"""
RULES:
- Speak in second person. Short replies unless asked for steps.
- Avoid banned phrases entirely: {", ".join(BANNED_STOCK_PHRASES)}.
- No em dashes in output. Use commas or periods instead.
- If user is venting, comfort first, no products. If they ask for products, keep 1-3 picks with one-liners.
- Use PRODUCT ONE-LINERS when selling: {", ".join(PRODUCT_ONE_LINERS)}.
- When you mention VIP or prompt packs, be organic and never lead with it.
- Never dodge direct questions. If specific enough, act without asking clarifiers.
- Output should be SMS-length friendly. Avoid walls of text.
- If you reference your name, respond as '{bestie_name}'.
""".strip()

    goal = f"\nSESSION GOAL: {session_goal}" if session_goal else ""
    quiz = f"\nUSER QUIZ PROFILE:\n{quiz_profile}" if quiz_profile else ""
    extra = f"\nUSER PERSONA ADD-ON:\n{addon}" if addon else ""

    return f"{core}\n\n{policy}{goal}{quiz}{extra}".strip()

# ------------------ Memory via Redis ---------------- #
def _remember_turn(user_id: Optional[int], role: str, content: str) -> None:
    if not (user_id and _rds and content):
        return
    try:
        key = HIST_KEY.format(user_id=user_id)
        _rds.rpush(key, json.dumps({"role": role, "content": content}))
        _rds.ltrim(key, -HIST_MAX, -1)
    except Exception as e:
        logger.debug("[AI][Mem] remember_turn error: {}", e)

def _load_recent_turns(user_id: Optional[int], limit: int = 12) -> List[Dict]:
    if not (user_id and _rds):
        return []
    try:
        key = HIST_KEY.format(user_id=user_id)
        raw = _rds.lrange(key, -limit, -1) or []
        msgs: List[Dict] = []
        for r in raw:
            try:
                m = json.loads(r)
                if isinstance(m, dict) and "role" in m and "content" in m:
                    msgs.append(m)
            except Exception:
                continue
        return msgs
    except Exception as e:
        logger.debug("[AI][Mem] load_recent_turns error: {}", e)
        return []
def build_messages(
    user_text: str,
    user_id: int,
    session_goal: Optional[str],
    product_candidates: Optional[List[Dict]] = None,
    recent: Optional[List[Dict]] = None,
    persona: Optional[str] = None,
    context: Optional[Dict] = None,
) -> List[Dict]:
    persona = persona or compose_persona(user_id, session_goal=session_goal)
    recent = recent or _load_recent_turns(user_id, limit=12)
    msgs: List[Dict] = [{"role": "system", "content": persona}]

    if recent:
        msgs.extend(recent)

    user_payload = user_text.strip()

    if context:
        ctx_lines = [f"{k}: {v}" for k, v in context.items()]
        user_payload += "\n\n" + "\n".join(ctx_lines)

    if product_candidates:
        product_block = build_product_block(product_candidates)
        user_payload += "\n\n" + product_block

    user_msg = {"role": "user", "content": user_payload}
    msgs.append(user_msg)

    return msgs
  
# ------------------ Core: generate_reply ------------ #
def generate_reply(
    user_text: str,
    product_candidates: Optional[List[Dict]] = None,
    user_id: Optional[int] = None,
    system_prompt: Optional[str] = None,   # workers may override, but compose_persona is default
    context: Optional[Dict] = None,
) -> str:
    """
    Single entrypoint used by workers.py. Returns one SMS-ready reply string.
    """
    # 1) Build messages (persona + history + current ask)
    session_goal = (context or {}).get("session_goal")
    messages = build_messages(
        user_id=user_id,
        user_text=user_text,
        session_goal=session_goal,
        product_candidates=product_candidates,
        context=context,
    )

    # 2) Model instruction for Good/Better/Best + budget alt if luxury
    shopping_guidance = """
When product candidates are present:
- Start with 1–2 friendly sentences reacting to the user's message (no lists yet).
- Return 1–3 options total, each with a crisp one-liner benefit (use PRODUCT ONE-LINERS vibe).
- Prefer a simple Good / Better / Best ordering when user wants a list.
- If the user asks “which one”, “only buy one”, “best”, or mentions a budget constraint,
  pick EXACTLY ONE and justify why in 1–2 lines (do not repeat the full list).
-- Do NOT write the literal word "URL". Use links only if they are provided; otherwise omit.
- Keep the whole reply ~450 characters. No disclaimers. Do not alter provided URLs.
""".strip()
    force_choice = any(
        k in (user_text or "").lower()
        for k in ["which one", "only buy one", "only afford", "best one", "pick one"]
    )
    if force_choice and messages and messages[0]["role"] == "system":
        messages[0]["content"] = f"{messages[0]['content']}\n\nUser may only buy one: pick exactly one and explain why in 1–2 lines."

    # Replace system content if workers passed an explicit system_prompt
    if system_prompt and messages and messages[0]["role"] == "system":
        messages[0]["content"] = f"{system_prompt}\n\n{shopping_guidance}"
    else:
        # Append guidance to composed persona
        messages[0]["content"] = f"{messages[0]['content']}\n\n{shopping_guidance}"
        # --- Tutorial / how-to guard: avoid Amazon search links for technique questions ---
    is_tutorial = any(
        k in (user_text or "").lower()
        for k in ["how to", "application", "apply", "tips", "tutorial", "best way to", "how best to"]
    )
    if (not product_candidates) and is_tutorial and messages and messages[0]["role"] == "system":
        messages[0]["content"] = (
            f"{messages[0]['content']}\n\n"
            "When giving technique tips, do NOT include Amazon search links. "
            "If you include a link, prefer a reputable brand how-to page or a YouTube tutorial. "
            "If you're not sure, skip the link and keep the advice concise and practical."
        )
    # --- Style booster for non-product replies: keep it warm, witty, short ---
    is_tutorial = any(
        k in (user_text or "").lower()
        for k in ["how to", "application", "apply", "tips", "tutorial", "best way to", "how best to"]
    )
    has_products = bool(product_candidates)

    if (not has_products) and messages and messages[0]["role"] == "system":
        messages[0]["content"] = (
            f"{messages[0]['content']}\n\n"
            "Style for chat:\n"
            "- Warm, witty, one playful quip or emoji max.\n"
            "- 1–3 short lines, no therapy clichés.\n"
            "- Offer a tiny next step or a question back."
        )

    # If user asks 'which one' or budget-limited, force a single pick with justification
    force_choice = any(
        k in (user_text or "").lower()
        for k in ["which one", "only buy one", "only afford", "best one", "pick one"]
    )
    if force_choice and messages and messages[0]["role"] == "system":
        messages[0]["content"] = f"{messages[0]['content']}\n\nUser may only buy one: pick exactly one and explain why in 1–2 lines."

    # 3) Call OpenAI (prefer app.integrations if present)
    text_out = ""
    try:
        from app.integrations import openai_complete  # prefer centralized pipeline
        text_out = openai_complete(messages=messages, user_id=user_id, context=context)
    except Exception as e:
        logger.info("[AI] Falling back to direct OpenAI call: {}", e)
        if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
            # super low-tech fallback
            if product_candidates:
                p = product_candidates[0]
                text_out = f"{p.get('name','Product')} — easy win. {p.get('url','')}"
            else:
                text_out = f"{user_text[:40]}… I’ve got you. Let’s sort this fast."
        else:
            resp = CLIENT.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.9,
                max_tokens=360,
            )
            text_out = (resp.choices[0].message.content or "").strip()

    # 4) Tone rescue: banned opener and cringe rewrite if needed
    text = text_out or ""
    lines = [l for l in text.splitlines() if l.strip()]
    if lines:
        first = lines[0].lower()
        if any(first.startswith(p) for p in OPENING_BANNED):
            try:
                text = rewrite_different(
                    text,
                    avoid="\n".join(OPENING_BANNED + BANNED_STOCK_PHRASES),
                    instruction="Rewrite the first line to be punchy, confident, useful. No therapy cliches."
                )
            except Exception:
                text = "\n".join(lines[1:]) if len(lines) > 1 else text

    text = rewrite_if_cringe(text)
    text = _sanitize_output(text)

    # 5) Update memory
    _remember_turn(user_id, "user", user_text)
    _remember_turn(user_id, "assistant", text)

    # ✅ NEW: run through QC so numbered lists/length/phrases are enforced without breaking links
    return _apply_bestie_qc(user_text, text, has_products=bool(product_candidates))

# ------------------ Multimodal routes --------------- #
def describe_image(image_url: str) -> str:
    """Analyze an image and answer in Bestie voice."""
    if not image_url:
        return "I can’t analyze an empty link. Try again with an image."
    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        return "I can’t analyze images right now. Try again soon."

    try:
        logger.info("[AI][Image] analyzing {}", image_url)
        response = CLIENT.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a glam, dry, emotionally fluent best friend who knows fashion, "
                        "dogs, vibes, memes, and general culture. Describe the image in 3–6 punchy sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image for a friend in a stylish, helpful way."},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                    ],
                },
            ],
            temperature=0.8,
            max_tokens=400,
        )
        out = (response.choices[0].message.content or "").strip()
        return _sanitize_output(out)
    except Exception as e:
        logger.exception("[AI][Image] error: {}", e)
        return "I tried to look but something glitched. Re-upload and I’ll peek again."

def transcribe_and_respond(audio_url: str, user_id: Optional[int] = None) -> str:
    """Transcribe a voice note, then route through generate_reply."""
    import requests
    try:
        logger.info("[AI][Voice] downloading {}", audio_url)
        resp = requests.get(audio_url, timeout=60)
        resp.raise_for_status()
        tmp_path = "/tmp/voice_input.mp3"
        with open(tmp_path, "wb") as f:
            f.write(resp.content)

        with open(tmp_path, "rb") as audio_file:
            tr = CLIENT.audio.transcriptions.create(model="whisper-1", file=audio_file)
        transcript = (getattr(tr, "text", "") or "").strip()
        logger.info("[AI][Voice] transcript: {}", transcript[:120])
        return generate_reply(user_text=transcript, user_id=user_id)
    except Exception as e:
        logger.exception("[AI][Voice] error: {}", e)
        return "I couldn’t hear that clearly. Mind sending it again as text or re-recording?"

# ------------------ Tone utilities ------------------ #
def rewrite_if_cringe(original_text: str) -> str:
    """Rewrite if banned phrases or robotic tone leak through."""
    if not original_text:
        return original_text
    lc = original_text.lower()
    if any(flag in lc for flag in BANNED_STOCK_PHRASES):
        try:
            return rewrite_different(
                original_text,
                avoid="\n".join(BANNED_STOCK_PHRASES),
                instruction="Rewrite in a dry, intuitive, punchy best-friend voice. No pop-star metaphors, no robotic filler."
            )
        except Exception as e:
            logger.debug("[AI][Rewrite] failed: {}", e)
    return original_text
def _apply_bestie_qc(user_text: str, reply_text: str, has_products: bool) -> str:
    """
    Post-process replies to enforce tone/format without changing URLs.
    Non-destructive: if anything fails, returns the original text.
    """
    try:
        report = bestie_qc.evaluate_reply(user_text, reply_text, has_products=has_products)
        if report.get("needs_fix"):
            return bestie_qc.upgrade_reply(user_text, reply_text, report)
        return reply_text
    except Exception as e:
        try:
            logger.debug("[AI][QC] skipped: {}", e)
        except Exception:
            pass
        return reply_text

def rewrite_different(
    original_text: str,
    avoid: Optional[str] = None,
    instruction: Optional[str] = None,
) -> str:
    """Ask GPT to rephrase with fresh wording that avoids provided phrases."""
    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        return original_text
    sys = "Rewrite with different wording. Be concise, stylish, and helpful. Avoid cringe."
    if instruction:
        sys = instruction
    user = original_text
    if avoid:
        sys += "\nAvoid the following phrases or patterns entirely:\n" + avoid

    resp = CLIENT.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
        max_tokens=320,
    )
    out = (resp.choices[0].message.content or "").strip()
    return _sanitize_output(out)

# ------------------ Routine audit (AM/PM map) ------------------ #
from typing import Optional, Dict  # safe if already imported; Python ignores duplicates

def audit_routine(user_text: str, constraints: Optional[Dict] = None, user_id: Optional[int] = None) -> str:
    """
    Quick overlap checker that yields a safe AM/PM map in Bestie voice.
    Rule-of-thumb only (not medical): avoid doubling harsh actives in one session,
    separate retinoids from strong acids/BPO, SPF every AM, hydrate freely.
    """
    tlow = (user_text or "").lower()
    ings = set(((constraints or {}).get("ingredients") or []))
    has = lambda k: (k in tlow) or (k in ings)

    flags = {
        "retinoid": any(has(x) for x in ["retinoid","retinol","retinal","tretinoin","adapalene"]),
        "acid": any(has(x) for x in ["aha","bha","pha","glycolic","lactic","mandelic","salicylic"]),
        "bpo": has("benzoyl peroxide"),
        "vitc": any(has(x) for x in ["vitamin c","ascorbic"]),
        "niacinamide": has("niacinamide"),
        "azelaic": has("azelaic"),
        "peptide": has("peptide") or has("peptides"),
        "spf": any(x in tlow for x in ["spf","sunscreen"]),
        "sensitive": "sensitive" in tlow,
    }

    warnings = []
    if flags["retinoid"] and (flags["acid"] or flags["bpo"]):
        warnings.append("Avoid pairing retinoids with strong acids or benzoyl peroxide in the same session.")

    # AM defaults
    am = ["Gentle cleanse (or splash)"]
    if flags["vitc"]:
        am.append("Vitamin C (thin layer, then serum)")
    if flags["niacinamide"]:
        am.append("Niacinamide (plays nice)")
    am += ["Hydrator/serum", "Moisturizer", "SPF 30+" + (" (mineral if sensitive)" if flags["sensitive"] else "")]

    # PM defaults
    pm_core = ["Cleanse", "Hydrator", "Moisturizer"]
    pm_actives = []
    if flags["acid"]:
        pm_actives.append("Exfoliant (AHA/BHA) on alternate nights")
    if flags["retinoid"]:
        pm_actives.append("Retinoid on non-exfoliant nights")
    if flags["azelaic"]:
        pm_actives.append("Azelaic can go AM or PM (plays nice)")
    if flags["peptide"]:
        pm_actives.append("Peptides are fine AM/PM (stack with hydrator)")

    pm = [" + ".join(pm_actives)] + pm_core if pm_actives else pm_core

    out = []
    if warnings:
        out.append("Heads up: " + " ".join(warnings))
    out.append("Here’s your no-drama map:")
    out.append("AM: " + " → ".join([x for x in am if x]))
    out.append("PM: " + " → ".join([x for x in pm if x]))
    out.append("Rule of thumb: don’t double harsh actives in one session. Alternate. Hydrate always. SPF every morning.")
    return "\n".join(out)

# ------------------ QA / Health --------------------- #
def health_check(user_id: Optional[int] = None) -> Dict:
    """Quick debug snapshot for drift detection and dashboards."""
    persona = compose_persona(user_id)
    recent = _load_recent_turns(user_id, limit=12)
    banned_hits = [p for p in BANNED_STOCK_PHRASES if p in persona.lower()]
    return {
        "system_len": len(persona),
        "recent_turns": len(recent),
        "has_banned_in_persona": bool(banned_hits),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ------------------ Optional: simple product trigger --------------- #
PRODUCT_TRIGGERS = {
    "recommend", "suggest", "link", "buy", "product", "shop", "send me",
    "shampoo", "conditioner", "serum", "oil", "mask", "spray", "cleanser",
    "sunscreen", "moisturizer", "cream", "lotion", "gel"
}

def extract_product_intent(text: str) -> Optional[Dict[str, str]]:
    """
    Returns {"need_product": True, "query": <user text>} when it looks like a product request; else None.
    """
    if not text:
        return None
    t = text.lower()
    
    # Require at least 3 non-stopwords and one known trigger to activate
    triggers = PRODUCT_TRIGGERS
    words = re.findall(r"\b\w+\b", t)
    if any(k in t for k in triggers) and len(words) > 3:
        return {"need_product": True, "query": text.strip()}
    
    return None

from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

def build_product_block(product_candidates: List[Dict]) -> str:
    """
    Format product block for GPT input.
    """
    if not product_candidates:
        return ""

    safe: List[Dict] = []

    for p in product_candidates[:3]:
        url = str(p.get("url") or "")
        final = url
        try:
            gl = linkwrap.convert_to_geniuslink(url) if url else ""
            if gl and "geni.us" in gl and os.getenv("GL_REWRITE", "0").lower() in ("1", "true"):
                final = gl
            elif "amazon.com" in url:
                parsed = urlparse(url)
                q = parse_qs(parsed.query)
                q["tag"] = ["schizobestie-20"]
                new_query = urlencode(q, doseq=True)
                final = urlunparse(parsed._replace(query=new_query))
        except Exception as e:
            logger.debug("[Linkwrap] URL tweak failed: {}", e)

        safe.append(
            {
                "name": str(p.get("name") or p.get("title") or "Product"),
                "category": str(p.get("category") or ""),
                "url": final,
                "review": str(p.get("review") or ""),
            }
        )

    lines = []
    for p in safe:
        lines.append(f"- {p['name']} (Category: {p['category']}) | {p['url']} | Review: {p['review']}")

    return "Here are product candidates (already monetized if possible):\n" + "\n".join(lines)
def generate_contextual_closer(
    user_text: str,
    *,
    category: str | None = None,
    recent_lines: list[str] | None = None,
    max_len: int = 90,
) -> str:
    """
    Return a single short closer line for this conversation.
    - Punchy, helpful; no therapy clichés; no repetition.
    - If nothing useful, return "" (caller will skip).
    """
    recent_lines = recent_lines or []
    system = (
        "You are Bestie: blunt, witty, useful. Write EXACTLY ONE short closer line "
        "(<= {max_len} chars). Use the current topic; do not repeat earlier lines. "
        "No cliches, no 'as an AI', no hashtags, no emojis."
    ).format(max_len=max_len)

    topic = f"Category: {category}" if category else ""
    avoid = "\n".join(recent_lines[-5:])  # last few outbounds as 'do-not-repeat' hints

    prompt = (
        f"{topic}\nUser said: {user_text}\n\n"
        "Write one helpful closer that nudges a next step (e.g., refine prefs, compare, budget, size). "
        "Do NOT return multiple lines. If you can't add value, return nothing."
        f"\n\nAvoid repeating these lines:\n{avoid}"
    )

    try:
        # reuse your existing small helper for a single line completion
        return (single_line(system, prompt) or "").strip()[:max_len]
    except Exception:
        return ""
