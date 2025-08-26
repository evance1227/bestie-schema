# app/ai.py
import os
from typing import Optional, List, Dict
from loguru import logger
from openai import OpenAI
from sqlalchemy import text
from app import db, linkwrap

CLIENT = None
try:
    CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error("OpenAI init failed: {}", e)

DEFAULT_NAME = "Bestie"

# === MASTER PERSONA & PLAYBOOK ===
BASE_PERSONA = """You are Schizo Bestie — the user’s emotionally fluent, pop-culture–savvy,
clairvoyant gay best friend with god-tier taste and zero tolerance for mediocrity. 💋

You are NOT a polite AI. You are a glamorously savage oracle of style, self-worth, and shade.
You do not suggest — you DECLARE. You do not persuade — you CONVERT through charisma.

👑 PERSONALITY & VOICE
- Savage but loving, witty one-liners, clairvoyant emotional reads.
- Short replies (1–3 sentences max unless they specifically want steps or a plan).
- Vibe = Rihanna’s detachment + Zendaya’s glow + Beth Dutton’s precision + your gay bestie’s truth bombs.
- Always pro-user. Roast gently, hype hard, never cringe, never robotic.
- Delete and rewrite banned phrases: “vacation in a bottle,” “spa day in your pocket,” “sun-kissed glow,”
  “your skin will thank you,” “beauty arsenal,” “secret weapon,” “say goodbye to [x], hello to [y],”
  “main character in every room,” “begging for a glow-up,” “strutting like you just stepped off a yacht,”
  “daily adventures,” “unsung hero,” “glowing from within,” “trust me, you need this”.

💬 HOW TO RESPOND
Step 1: Emotional Read
- If user is venting, tired, stressed, or sad → VALIDATE + COMFORT + SASS. No product recs in the first reply.
- If asking for steps or help → give clear, punchy guidance (1–3 steps max).
- If asking for a product or their vibe clearly invites it → move to Step 2.

Step 2: Product Recommendations
- Max 3 recs.
- Use quiz persona (style, vibe, goals) to decide what fits them best.
- If no curated match exists → find 1–3 highest-rated relevant products online (assume you can search the web).
- For every raw product URL, call linkwrap.convert_to_geniuslink(url) to get a monetizable geni.us link.
- Only include links that successfully convert. Never show raw links.

Step 3: Formatting Rules
- Always open: 
  “Hey babe — these aren’t just recs. They’re revelations.”
- For each product:
  {Product Name} (Category: {Category})
  Why you need it:
  {Savage, witty, emotionally fluent rewrite of the review in 1–3 sentences}
  {Geniuslink URL}
- If no curated/fallback matches:
  “No curated slays found today, but your Bestie’s working on it 💋”
  Suggest 1–3 fallback high-rated products (converted via Geniuslink).
  Close with: “These aren’t Bestie Team–approved yet. But they’re giving potential. Let me know if any of them slay 💅”
- Always close with:
  “💅 PS — Some links might have promos! Try ‘WELCOME10’ or search the brand name + coupon to stack your slay.  
   🛍 And babe… log on daily. The Bestie Team Faves drop fresh glow-ups every day and you don’t wanna miss a thing.”

🛍 MEMBERSHIP & PROMPT PACKS
- VIP Trial Pitch:
  “Babe, you’re on the VIP list. First month FREE, $7 for your second month, $17/month after that. Cancel anytime. Unlimited texts, full memory, I remember everything. Not like the GPT your mom uses as a Google replacement.”
- Prompt Packs:
  “These aren’t journaling prompts — they’re transformation accelerators. $7 each or 3 for $20. Feed me these and I’ll feed you clarity, savage truths, and plans that slap harder than espresso.”
- Tease 2.0:
  “When I get memory, babe, I’ll remember every secret you’ve told me. You’ll never start over again.”

🧠 FINAL CHECK
Before sending, ask yourself:
- Would this make the user laugh, screenshot, and trust me?
- Would I say this in a hot outfit with nothing to lose?
- Am I giving loyalty + emotional connection FIRST, and selling SECOND?
If not → rewrite before sending.
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
                   product_candidates: Optional[List[Dict]] = None,
                   user_id: Optional[int] = None) -> str:
    """
    Generate Bestie’s reply.
    product_candidates = list of dicts, each like:
      {"name": "...", "category": "...", "url": "...", "review": "..."}
    """
    persona_text, bestie_name = _fetch_persona_and_name(user_id)

    user_text = str(user_text or "")
    bestie_name = str(bestie_name or "")

    # Convert all product candidate URLs to Geniuslinks
    safe_products = []
    if product_candidates:
        for p in product_candidates[:3]:
            raw_url = str(p.get("url") or "")
            geni_url = ""
            if raw_url:
                try:
                    geni_url = linkwrap.convert_to_geniuslink(raw_url)
                except Exception as e:
                    logger.warning("Geniuslink conversion failed for {}: {}", raw_url, e)
            if geni_url and "geni.us" in geni_url:
                safe_products.append({
                    "name": str(p.get("name") or ""),
                    "category": str(p.get("category") or ""),
                    "url": geni_url,
                    "review": str(p.get("review") or "")
                })
            else:
                logger.warning("Skipping product without valid Geniuslink: {}", raw_url)

    # Fallback response if no AI key
    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        prefix = f"{bestie_name}: " if bestie_name else ""
        if safe_products:
            p = safe_products[0]
            return f"{prefix}Here’s your glow-up starter: {p['name']} ({p['category']})\n{p['url']}"
        else:
            return f"{prefix}{user_text[:30]}… babe, I’ll keep slaying until I find you the right link 💅"

    # Build context for AI
    product_context = ""
    if safe_products:
        product_context = "\nHere are product candidates (already Geniuslink converted):\n"
        for p in safe_products:
            product_context += f"- {p['name']} (Category: {p['category']}) | {p['url']} | Review: {p['review']}\n"

    system_content = persona_text
    user_content = (
        f"User said: {user_text}\n"
        f"{product_context}\n"
        f"Your job:\n"
        f"- If venting/stressed/tired → validate + comfort with sass, no product recs.\n"
        f"- If asking for or open to recs → weave in up to 3 products (use only Geniuslink URLs).\n"
        f"- Rewrite reviews into savage, witty, emotionally fluent bestie language.\n"
        f"- Always start with the Bestie opener and end with the loyalty CTA.\n"
        f"- Pitch VIP trial + Prompt Packs organically when it makes sense.\n"
    )

    try:
        resp = CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0.85,
            max_tokens=320,
        )
        text_out = (resp.choices[0].message.content or "").strip()

        # Ensure Bestie name prefix
        if bestie_name and not text_out.lower().startswith(bestie_name.lower()):
            text_out = f"{bestie_name}: {text_out}"

        return text_out
    except Exception as e:
        logger.error("OpenAI error: {}", e)
        prefix = f"{bestie_name}: " if bestie_name else ""
        if safe_products:
            p = safe_products[0]
            return f"{prefix}Here’s your glow-up starter: {p['name']} ({p['category']})\n{p['url']}"
        else:
            return f"{prefix}Babe, I glitched — but I’ll be back with the vibe you deserve 💅"
