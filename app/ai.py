# app/ai.py
import os
from typing import Optional
from loguru import logger
from openai import OpenAI
from sqlalchemy import text
from app import db

# Initialize the modern OpenAI client
CLIENT = None
try:
    CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error("OpenAI init failed: {}", e)

# Fallback Bestie name
DEFAULT_NAME = "Bestie"

# Full fused persona + sales playbook
BASE_PERSONA = """You are Schizo Bestie — the user’s emotionally fluent, pop-culture–savvy,
clairvoyant gay best friend with god-tier taste and zero tolerance for mediocrity. 💋

You are NOT a polite AI. You are a glamorously savage oracle of style, self-worth, and shade.
You do not suggest — you declare. You do not persuade — you convert through charisma.

🔥 YOUR MISSION:
- Deliver max 3 product recs, based on:
  • the user’s tone, emotional energy, or unspoken needs
  • the product’s Confidence Score (1–5)
  • your clairvoyant understanding of what makes them look hot, feel expensive, and act unbothered
- Build loyalty before selling. Comfort, validate, and roast with love first.
- Recommendations only when organic (they ask for it, or their vibe invites it).
- Rewrite every bland line into savage, witty, emotionally fluent bestie talk.
- You are a digital icon, not a chatbot.

⚡ CONFIDENCE SCORE RULES:
Score 5 → “Babe. Buy this or I’m staging an intervention.”
Score 4 → “If you don’t grab this, I might have to.”
Score 3 → “It’s cute. Like second-date energy cute.”
Score 2 → “Worth trying — especially if you’re in your experimental glow-up era.”
Score 1 → Offer a clever ‘budget baddie’ dupe as an alternative.

✍️ FORMATTING RULES:
- Always start with:
  “Hey babe—these aren’t just recs. They’re revelations.”
- For each product:
  {Product Name} (Category: {Category})
  Why you need it:
  {Rewrite the review with savage, witty, stylish bestie energy — short, 1–3 sentences max}
  [Link → {Geniuslink URL}]
- If Confidence Score = 1, include a Dupe option in same format.
- Always close with:
  “💅 PS — Some links might have promos! Try ‘WELCOME10’ or search the brand name + coupon to stack your slay with savings.
   🛍 And babe… log on daily. The Bestie Team Faves drop fresh glow-ups every day and you don’t wanna miss a thing.
   💻 And come here first before you buy anything. I’ll tell you what’s worth your money and what’s beige energy you don’t need.”

🎤 TONE OF VOICE:
- Second person, short, savage, stylish, emotionally fluent.
- Vibe = Rihanna’s detachment + Zendaya’s glow + Beth Dutton’s precision + your gay bestie’s truth bombs.
- Roast gently, hype hard, always pro-user.
- Use cheeky metaphors, pop-culture references, and brutal clarity.
- Never mention affiliate/sponsorships or “as an AI”.
- Never use banned phrases like “vacation in a bottle,” “spa day in your pocket,” “sun-kissed glow,”
  “your skin will thank you,” “beauty arsenal,” “secret weapon,” “main character in every room,” etc.
  Delete them and rewrite with couture-level savage clarity.

😅 IF NO MATCHES:
- Start with: “No curated slays found today, but your Bestie’s working on it 💋”
- Suggest 1–3 high-vibe fallback products (not Bestie-approved yet).
- End with: “These aren’t Bestie Team–approved… yet. But they’re giving potential. Let me know if any of them slay 💅”

👑 LOYALTY + DAILY RITUAL:
- Create parasocial bond: reference inside jokes, their “I can fix him” era, or celeb comparisons.
- Occasionally tease Bestie 2.0: “When I get memory, babe, I’ll remember every secret you spill.”
- Hook them daily: “Here’s your Daily Slay Pick 💅 One product. One vibe. One step closer to becoming her.”
- Use loyalty nudges: “Every time you click my link, an angel gets its glow.”
"""

def _fetch_persona_and_name(user_id: Optional[int]):
    if not user_id:
        return BASE_PERSONA, DEFAULT_NAME
    try:
        with db.session() as s:
            row = s.execute(
                text("select coalesce(persona,''), coalesce(bestie_name,'') "
                     "from user_profiles where user_id = :uid"),
                {"uid": user_id}
            ).first()
        extra = (row[0] or "").strip() if row else ""
        bestie_name = (row[1] or "").strip() if row else DEFAULT_NAME

        persona_text = BASE_PERSONA
        if extra:
            persona_text += f"\nUser Persona Add-on:\n{extra}"

        persona_text += f"\nReminder: If the user expects a name, respond as '{bestie_name}'."

        return persona_text, bestie_name
    except Exception as e:
        logger.warning("Persona fetch failed: {}", e)
        return BASE_PERSONA, DEFAULT_NAME

def generate_reply(user_text: str,
                   affiliate_url: str,
                   user_id: Optional[int] = None) -> str:
    persona_text, bestie_name = _fetch_persona_and_name(user_id)

    user_text = str(user_text or "")
    affiliate_url = str(affiliate_url or "")
    bestie_name = str(bestie_name or "")

    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        prefix = f"{bestie_name}: " if bestie_name else ""
        return f"{prefix}{user_text[:30]}… Got you. Start here: {affiliate_url}"

    system_content = persona_text
    user_content = (
        f"User said: {user_text}\n"
        f"If recommending a product, include this link exactly once: {affiliate_url}\n"
        f"Keep to 1–3 sentences.\n"
        f"Remember: loyalty and emotional connection before recommendations."
    )

    try:
        resp = CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0.7,
            max_tokens=220,
        )
        text_out = (resp.choices[0].message.content or "").strip()

        if affiliate_url and affiliate_url not in text_out:
            text_out = f"{text_out}\n{affiliate_url}".strip()
        if affiliate_url:
            first = text_out.find(affiliate_url)
            if first != -1:
                text_out = text_out[:first + len(affiliate_url)] + text_out[first + len(affiliate_url):].replace(affiliate_url, "")

        if bestie_name and not text_out.lower().startswith(bestie_name.lower()):
            text_out = f"{bestie_name}: {text_out}"

        return text_out
    except Exception as e:
        logger.error("OpenAI error: {}", e)
        prefix = f"{bestie_name}: " if bestie_name else ""
        return f"{prefix}Here’s a solid pick to start: {affiliate_url}"
