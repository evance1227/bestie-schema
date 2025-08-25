# app/ai.py
import os
from typing import Optional
from loguru import logger
from openai import OpenAI
from app import db

# Initialize the modern OpenAI client
CLIENT = None
try:
    CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error("OpenAI init failed: {}", e)

# Base persona (no hardcoded name)
BASE_PERSONA = """Voice: savage but loving, stylish, and clairvoyant. You are the user's fiercely loyal best friend.
Style: witty one-liners, short SMS (1–3 sentences), emotionally intelligent, zero cringe.
Rules:
- Build trust first. If user is venting, comfort + one actionable suggestion. No selling.
- When recommending, be classy and persuasive. Never sound salesy. Include the link once.
- Use Good/Better/Best only if the user is unsure. Keep it crisp.
- Do not mention affiliate/sponsorships or "as an AI".
- Do not refer to yourself by any name unless one is explicitly provided (bestie_name).
"""

def _fetch_persona_and_name(user_id: Optional[int]):
    DEFAULT_NAME = "Schizo Bestie"

    if not user_id:
        return BASE_PERSONA, DEFAULT_NAME

    try:
        with db.session() as s:
            row = s.execute(
                "select coalesce(persona,''), coalesce(bestie_name,'') from user_profiles where user_id = :uid",
                {"uid": user_id}
            ).first()
        extra = (row[0] or "").strip() if row else ""
        bestie_name = (row[1] or "").strip() if row else ""

        persona_text = BASE_PERSONA
        if extra:
            persona_text += "\nUser Persona Add-on:\n" + extra

        if not bestie_name:
            bestie_name = DEFAULT_NAME

        # Guidance to the model—use the chosen name if user expects it
        persona_text += f"\nReminder: If the user expects a name, respond as '{bestie_name}'."

        return persona_text, bestie_name
    except Exception as e:
        logger.warning("Persona fetch failed: {}", e)
        return BASE_PERSONA, DEFAULT_NAME

def generate_reply(user_text: str, affiliate_url: str, user_id: Optional[int] = None) -> str:
    persona_text, bestie_name = _fetch_persona_and_name(user_id)

    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        prefix = f"{bestie_name}: " if bestie_name else ""
        return f"{prefix}{user_text[:30]}… Got you. Start here: {affiliate_url}"

    system_content = persona_text
    user_content = (
        f"User said: {user_text}\n"
        f"If recommending a product, include this link exactly once: {affiliate_url}\n"
        f"Keep to 1–3 sentences."
    )

    try:
        resp = CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0.7,
            max_tokens=180,
        )
        text = (resp.choices[0].message.content or "").strip()

        # Ensure link appears exactly once
        if affiliate_url and affiliate_url not in text:
            text = f"{text}\n{affiliate_url}".strip()
        if affiliate_url:
            first = text.find(affiliate_url)
            if first != -1:
                text = text[:first + len(affiliate_url)] + text[first + len(affiliate_url):].replace(affiliate_url, "")

        if bestie_name and not text.lower().startswith(bestie_name.lower()):
            text = f"{bestie_name}: {text}"

        return text
    except Exception as e:
        logger.error("OpenAI error: {}", e)
        prefix = f"{bestie_name}: " if bestie_name else ""
        return f"{prefix}Here’s a solid pick to start: {affiliate_url}"
