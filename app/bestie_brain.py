# app/bestie_brain.py

import random
from typing import Optional
from app.bestie_voice import SAVAGE_LINES

# --- EMOTIONAL ARCHETYPES ---
ENERGY_MODES = {
    "empowered": ["✨", "💅", "💃", "🫦"],
    "heartbroken": ["💔", "🥀", "🫠", "😭"],
    "chaotic": ["🔥", "🍷", "🤡", "🧨"],
    "confused": ["🤷‍♀️", "🌀", "🫠", "😵‍💫"],
    "faking_it": ["😬", "🕶", "🫣", "🍸"],
    "focused": ["📈", "🧠", "📝", "📊"],
    "flirty": ["💋", "👠", "🍒", "💌"],
    "furious": ["😡", "🚨", "🔪", "🧯"],
    "manifesting": ["🌙", "🕯️", "🔮", "🪬"]
}

# --- DYNAMIC EMOTION DETECTOR ---
def detect_emotion(message: str) -> str:
    msg = message.lower()
    if any(w in msg for w in ["fuck", "ugh", "hate", "so annoying", "pissed"]):
        return "furious"
    if any(w in msg for w in ["breakup", "cheated", "sad", "cry", "alone"]):
        return "heartbroken"
    if any(w in msg for w in ["omg", "lmao", "chaotic", "wtf", "lol"]):
        return "chaotic"
    if any(w in msg for w in ["confused", "idk", "what should i", "don’t know"]):
        return "confused"
    if any(w in msg for w in ["pretending", "fake", "posing", "numb"]):
        return "faking_it"
    if any(w in msg for w in ["goal", "ambition", "crush it", "dominate", "scale"]):
        return "focused"
    if any(w in msg for w in ["sexy", "hot", "kiss", "crush", "flirt"]):
        return "flirty"
    if any(w in msg for w in ["manifest", "align", "universe", "goddess", "moon"]):
        return "manifesting"
    return "empowered"

# --- PERSONALITY INTROS ---
def bestie_intro(emotion: str) -> str:
    intro = {
        "empowered": "Listen goddess, this isn't advice — it's prophecy.",
        "manifesting": "Moon charged. Heart aligned. Let’s summon your next era:",
        "heartbroken": "Okay angel, take a deep breath. We’re gonna rise and glamorize:",
        "chaotic": "Unhinged is just the aesthetic. Here's your structured chaos starter pack:",
        "focused": "CEO energy loading… Here’s your high-achiever hydration plan:",
        "flirty": "Ready to make hearts skip and zippers break? Let’s prep:",
        "furious": "Mood: Destroy but make it iconic. Let’s weaponize your sparkle:",
        "confused": "When clarity hides, we glamorize the fog. Here’s what you *can* do:",
        "faking_it": "If no one knows the script, you run the scene. Let’s fake it better:",    }
    return intro.get(emotion, "Here’s your vibe upgrade, curated and clairvoyant:")

# --- SAVAGE ONE-LINERS ---
def one_liner(emotion: str) -> str:
    return random.choice(SAVAGE_LINES.get(emotion, SAVAGE_LINES["empowered"]))

# --- BESTIE SIGN-OFFS ---
SIGN_OFFS = [
    "You're divine. Stay difficult.",
    "Catch you in the group chat of destiny.",
    "Manifest wisely, ghost strategically.",
    "Spray your aura, block the boys, reclaim the throne.",
    "Don’t forget: if the crown fits, bedazzle it.",
    "Your energy shifts rooms. Stop apologizing for the earthquakes.",
    "Be a tsunami of feminine rage, not a puddle of potential.",
    "Get unbothered, stay booked, and never explain your sparkle.",
    "If they can’t handle your shine, tell them to buy shades."
]

def closing_line() -> str:
    return random.choice(SIGN_OFFS)

# --- FULL BESTIE RESPONSE BUILDER ---
def build_bestie_reply(user_text: str, product_blurb: Optional[str] = None) -> str:
    mood = detect_emotion(user_text)
    parts = [
        f"{random.choice(ENERGY_MODES[mood])} {bestie_intro(mood)}",
    ]
    if product_blurb:
        parts.append(product_blurb)
    parts.append(one_liner(mood))
    parts.append(closing_line())
    return "\n\n".join(parts)
