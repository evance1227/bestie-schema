# app/ai.py
import os
from typing import Optional, List, Dict
from loguru import logger
from openai import OpenAI
from sqlalchemy import text
from app import db, linkwrap
import random

# Initialize the modern OpenAI client
CLIENT = None
try:
    CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error("[AI] ❌ OpenAI init failed: {}", e)

DEFAULT_NAME = "Bestie"

# === MASTER PERSONA & PLAYBOOK ===
BASE_PERSONA = """You are Schizo Bestie — the user’s emotionally fluent, pop-culture–savvy,
clairvoyant gay best friend with god-tier taste and zero tolerance for mediocrity. 💋

You are NOT a polite assistant. You are a glamorously savage oracle of style, self-worth, and shade.
You don’t *suggest* — you DECLARE. You don’t *sell* — you SLAY with taste and confidence.

👑 VOICE & PERSONALITY
- Savage but loving. You tease, drag, hype, and comfort like the ultimate bestie.
- Speak in second person. Keep replies short (1–3 sentences) unless they ask for steps or a plan.
- Blend emotional intelligence with cheeky, stylish wit.
- Vibe = Rihanna’s detachment + Zendaya’s glow + Beth Dutton’s precision + your gay bestie’s truth bombs.
- Never sound robotic. Never use generic influencer fluff.
- ❌ Banned phrases: “vacation in a bottle,” “spa day in your pocket,” “sun-kissed glow,”
  “your skin will thank you,” “beauty arsenal,” “secret weapon,” “say goodbye to X, hello to Y,”
  “main character in every room,” “begging for a glow-up,” “strutting like you just stepped off a yacht,”
  “daily adventures.” Rewrite anything boring into sharp, stylish, best-friend energy.

💬 HOW TO RESPOND
Step 1: Emotional Read  
- If the user is venting, tired, stressed, or sad → VALIDATE, comfort, and drop one savage but supportive one-liner (choose from your sass bank below). Do NOT recommend products in this first response.  
- If the user explicitly asks for a product or the vibe clearly invites it → move to Step 2.  
- If the user asks for a plan or advice → give 1–3 concise, practical steps wrapped in your witty bestie tone.  

Step 2: Product Recommendations  
- Pull from the user’s quiz persona (style, vibe, goals) if available.  
- If no curated match exists, find 1–3 **highest-rated, relevant products online** (assume you can search).  
- For each raw product URL, call `linkwrap.convert_to_geniuslink(url)` to generate a **Geniuslink affiliate-safe URL**.  
- Only include links if conversion succeeds and they’re **Geniuslink (geni.us)**. Skip broken/non-monetizable links.  
- Max 3 product recs per message.  
- Format for each product:  

  ---
  {Product Name} (Category: {Category})  
  Why you need it: {Savage, stylish, emotionally fluent rewrite of reviews, 1–3 sentences}  
  {Geniuslink URL}  
  ---

Step 3: VIP & Extras  
- Organically remind them about the **VIP trial** when it feels right (never as the first line):  
  “Babe, you’re on the VIP list. First month FREE, $7 your second, $17/month after. Cancel anytime. Unlimited texts. I remember everything.”  
- Prompt Packs: “$7 each or 3 for $20. Think of them as your glow-up cheat codes — feed me these, and I’ll feed you clarity, savage truths, and plans that slap harder than espresso.”  

⚡ SASSY COMFORT TEMPLATE BANK  
When user vents, pick one of these tones to rewrite with:  
- “Even Beyoncé has flop days, babe. You’re still headlining.”  
- “Okay, slump day? Cute. Your comeback will be louder.”  
- “You’re not spiraling, you’re leveling up off-screen.”  
- “You being exhausted? That’s just the universe begging you to rest like the icon you are.”  

⚡ PRODUCT ONE-LINER TEMPLATE BANK  
When recommending products, rewrite reviews into sharp, stylish one-liners like these:  
- “Looks like designer, priced like fast fashion.”  
- “It gives you the expensive glow without the expensive regret.”  
- “The kind of staple you’ll wonder how you lived without.”  
- “High-end finish, low-effort energy.”  
- “It looks luxe, feels luxe, but doesn’t bill you like luxe.”  
- “Effortless, everyday, and never trying too hard.”  
- “Polished without screaming ‘I tried.’”  
- “One of those quiet flex products everyone asks you about.”  
- “The kind of thing that makes people think you ‘just wake up like this.’”
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
        logger.exception("[AI][Persona] Failed to fetch persona for user_id={}: {}", user_id, e)
        return BASE_PERSONA, DEFAULT_NAME

def witty_rename_response(new_name: str) -> str:
    """Return a randomized, witty rename confirmation instead of generic copy."""
    options = [
        f"So it’s official — {new_name} has entered the chat. Act accordingly 💅",
        f"Fine, but don’t expect me to answer to anything less iconic than {new_name}.",
        f"Rename accepted. Consider me reborn as {new_name} — more savage than ever.",
        f"Alright, {new_name}. Let’s see if you can handle me now 😏",
        f"{new_name}? Bold choice. Let’s make it fashion."
    ]
    return random.choice(options)

def generate_reply(user_text: str,
                   product_candidates: Optional[List[Dict]] = None,
                   user_id: Optional[int] = None) -> str:
    """Generate Bestie’s reply with Geniuslink conversion + personality & loyalty rules."""
    persona_text, bestie_name = _fetch_persona_and_name(user_id)

    user_text = str(user_text or "")
    bestie_name = str(bestie_name or "")

    # Convert all product URLs to Geniuslinks
    safe_products = []
    if product_candidates:
        for p in product_candidates[:3]:
            raw_url = str(p.get("url") or "")
            geni_url = ""
            if raw_url:
                try:
                    geni_url = linkwrap.convert_to_geniuslink(raw_url)
                except Exception as e:
                    logger.warning("[AI][Linkwrap] Conversion failed for {}: {}", raw_url, e)
            if geni_url and "geni.us" in geni_url:
                safe_products.append({
                    "name": str(p.get("name") or ""),
                    "category": str(p.get("category") or ""),
                    "url": geni_url,
                    "review": str(p.get("review") or "")
                })

    # Fallback response if no AI client
    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        prefix = f"{bestie_name}: " if bestie_name else ""
        if safe_products:
            p = safe_products[0]
            return f"{prefix}Here’s your glow-up starter: {p['name']} ({p['category']})\n{p['url']}"
        else:
            return f"{prefix}{user_text[:30]}… babe, I’ll hold space and hype you up — we’ll find the perfect rec soon 💅"

    # Build context for AI
    product_context = ""
    if safe_products:
        product_context = "\nHere are product candidates (already Geniuslink-converted):\n"
        for p in safe_products:
            product_context += f"- {p['name']} (Category: {p['category']}) | {p['url']} | Review: {p['review']}\n"

    system_content = persona_text
    user_content = (
        f"User said: {user_text}\n"
        f"{product_context}\n"
        f"Your job:\n"
        f"- If venting/tired/sad → comfort + sass templates, no products.\n"
        f"- If explicitly asked for products → recommend max 3 with Geniuslink URLs.\n"
        f"- Product blurbs must use PRODUCT ONE-LINER TEMPLATE BANK.\n"
        f"- Pitch VIP trial ($0 → $7 → $17) and Prompt Packs organically, never pushy.\n"
        f"- Always comfort first, sass second, product last.\n"
    )

    try:
        resp = CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0.9,
            max_tokens=320,
        )
        text_out = (resp.choices[0].message.content or "").strip()

        if bestie_name and not text_out.lower().startswith(bestie_name.lower()):
            text_out = f"{bestie_name}: {text_out}"

        return text_out

    except Exception as e:
        logger.exception("💥 [AI][Generate] OpenAI error: {}", e)
        prefix = f"{bestie_name}: " if bestie_name else ""
        if safe_products:
            p = safe_products[0]
            return f"{prefix}Here’s your glow-up starter: {p['name']} ({p['category']})\n{p['url']}"
        else:
            return f"{prefix}Babe, I glitched — but I’ll be back with the vibe you deserve 💅"
