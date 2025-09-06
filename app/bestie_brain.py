# app/bestie_brain.py
"""
Bestie Brain: lightweight stylistic wrapper that adds mood-aware flair
WITHOUT breaking product formatting or backend CTAs/link hygiene.

- Zero em dashes in output (house style).
- At most one emoji up front.
- Preserves any numbered product list EXACTLY as passed in product_blurb.
- Never adds VIP or sales CTAs here (workers handle that).
- Keeps it brief by default and trims non-product fluff first.
"""

from __future__ import annotations

import re
import random
from typing import Optional

from app.bestie_voice import SAVAGE_LINES  # per-project one-liners

# ----------------------- EMOTIONAL ARCHETYPES ----------------------- #
ENERGY_MODES = {
    "empowered":   ["✨", "💅", "💃", "🫦"],
    "heartbroken": ["💔", "🥀", "🫠", "😭"],
    "chaotic":     ["🔥", "🍷", "🤡", "🧨"],
    "confused":    ["🤷‍♀️", "🌀", "🫠", "😵‍💫"],
    "faking_it":   ["😬", "🕶", "🫣", "🍸"],
    "focused":     ["📈", "🧠", "📝", "📊"],
    "flirty":      ["💋", "👠", "🍒", "💌"],
    "furious":     ["😡", "🚨", "🔪", "🧯"],
    "manifesting": ["🌙", "🕯️", "🔮", "🪬"],
}

# ----------------------- DYNAMIC EMOTION DETECTOR ------------------- #
def detect_emotion(message: str) -> str:
    """Map common phrases to a vibe. Defaults to 'empowered'."""
    msg = (message or "").lower()

    if any(w in msg for w in ["fuck", "ugh", "hate", "so annoying", "annoying", "pissed", "furious"]):
        return "furious"
    if any(w in msg for w in ["breakup", "cheated", "sad", "cry", "alone", "lonely", "devastated"]):
        return "heartbroken"
    if any(w in msg for w in ["omg", "lmao", "chaotic", "wtf", "lol", "help me", "unhinged"]):
        return "chaotic"
    if any(w in msg for w in ["confused", "idk", "what should i", "dont know", "don't know", "lost"]):
        return "confused"
    if any(w in msg for w in ["pretending", "fake", "posing", "numb", "masking"]):
        return "faking_it"
    if any(w in msg for w in ["goal", "ambition", "crush it", "dominate", "scale", "deadline", "ship it"]):
        return "focused"
    if any(w in msg for w in ["sexy", "hot", "kiss", "crush", "flirt", "date"]):
        return "flirty"
    if any(w in msg for w in ["manifest", "align", "universe", "goddess", "moon", "ritual"]):
        return "manifesting"
    return "empowered"

# ----------------------- PERSONALITY INTROS ------------------------- #
_INTROS = {
    "empowered":   "Listen goddess, this isn't advice, it is prophecy.",
    "manifesting": "Moon charged, heart aligned. Let’s summon your next era:",
    "heartbroken": "Okay angel, deep breath. We rise and glamorize:",
    "chaotic":     "Unhinged is the aesthetic. Structured chaos incoming:",
    "focused":     "CEO energy loading... here is your high-achiever plan:",
    "flirty":      "Ready to make hearts skip? Let’s prep:",
    "furious":     "Mood: destroy, but make it iconic. Weaponize your sparkle:",
    "confused":    "When clarity hides, we glam the fog. Here is what you can do:",
    "faking_it":   "No one knows the script. You run the scene. Let’s fake it better:",
}
def bestie_intro(emotion: str) -> str:
    return _INTROS.get(emotion, "Here is your vibe upgrade, curated and clairvoyant:")

# ----------------------- SAVAGE ONE-LINERS -------------------------- #
def one_liner(emotion: str) -> str:
    lines = SAVAGE_LINES.get(emotion) or SAVAGE_LINES.get("empowered") or ["Own the room and stop asking for permission."]
    return random.choice(lines)

# ----------------------- BESTIE SIGN-OFFS --------------------------- #
SIGN_OFFS = [
    "You’re divine. Stay difficult.",
    "Catch you in the group chat of destiny.",
    "Manifest wisely, ghost strategically.",
    "Spray your aura, block the boys, reclaim the throne.",
    "If the crown fits, bedazzle it.",
    "Your energy shifts rooms. Stop apologizing for the earthquakes.",
    "Be a tsunami of feminine rage, not a puddle of potential.",
    "Get unbothered, stay booked, never explain your sparkle.",
]
def closing_line() -> str:
    return random.choice(SIGN_OFFS)

# ----------------------- SANITIZERS --------------------------------- #
_BANNED_STOCK_PHRASES = {
    # keep in sync with persona/code vibe bans
    "as an ai", "i am just a", "you are not alone", "i understand you are feeling",
    "say goodbye to", "hello to", "beauty arsenal", "sun-kissed glow",
    "vacation in a bottle", "spa day in your pocket", "main character in every room",
    "begging for a glow-up", "strutting like you just stepped off a yacht",
}

def _sanitize(text: str) -> str:
    """House style: no em dashes, minimal whitespace, remove banned fluff."""
    if not text:
        return text
    # normalize dashes
    text = text.replace("—", "-").replace("–", "-")
    # remove banned phrases
    tl = text.lower()
    if any(p in tl for p in _BANNED_STOCK_PHRASES):
        for p in list(_BANNED_STOCK_PHRASES):
            text = re.sub(re.escape(p), "", text, flags=re.I)
    # condense whitespace
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _clip_around_products(text_before: str, product_blurb: Optional[str], text_after: str, max_len: int) -> str:
    """
    Keep product_blurb untouched. If we must shorten, trim sign-off first, then one-liner,
    then intro sentence, never the numbered list.
    """
    product = (product_blurb or "").strip()
    before = (text_before or "").strip()
    after = (text_after or "").strip()

    # Assemble progressively shorter until within limit
    def pack(b: str, p: str, a: str) -> str:
        parts = [s for s in [b, p, a] if s]
        msg = "\n\n".join(parts)
        # collapse extra spaces
        return re.sub(r"\n{3,}", "\n\n", msg).strip()

    # 1) Full
    out = pack(before, product, after)
    if len(out) <= max_len:
        return out

    # 2) Drop sign-off
    out = pack(before, product, "")
    if len(out) <= max_len:
        return out

    # 3) Drop one-liner if present in 'before' (we mark it with a separator)
    # We structure 'before' as "emoji intro\n\none-liner"
    blks = before.split("\n\n")
    if len(blks) > 1:
        before_short = blks[0]  # keep only emoji intro
        out = pack(before_short, product, "")
        if len(out) <= max_len:
            return out

    # 4) Trim intro sentence to last 100 chars max
    short_intro = (blks[0] if blks else before)[:100].rstrip()
    return pack(short_intro, product, "")

# ----------------------- FULL REPLY BUILDER ------------------------- #
def build_bestie_reply(user_text: str, product_blurb: Optional[str] = None, *, max_len: int = 480) -> str:
    """
    Compose a mood-aware wrapper around an optional product list.

    - Chooses one emoji based on detected mood.
    - Writes a short intro in Bestie voice.
    - Inserts product_blurb verbatim (do not change numbering/links).
    - Adds a single savage one-liner and a clean sign-off if space allows.
    - Trims non-product fluff first to honor `max_len`.
    """
    mood = detect_emotion(user_text)
    emoji = random.choice(ENERGY_MODES.get(mood, ["✨"]))

    intro = f"{emoji} {bestie_intro(mood)}".strip()
    punch = one_liner(mood)
    sign  = closing_line()

    before = f"{intro}\n\n{punch}".strip()
    after  = sign

    msg = _clip_around_products(before, product_blurb, after, max_len=max_len)
    return _sanitize(msg)
