# app/ai.py

import os
import re
import random
from typing import Optional, List, Dict

from loguru import logger
from openai import OpenAI
from sqlalchemy import text as sqltext

from app import db, linkwrap

# Initialize the OpenAI client
CLIENT = None
try:
    CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error("[AI] ❌ OpenAI init failed: {}", e)

DEFAULT_NAME = "Bestie"

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

PRODUCT_TRIGGERS = {
    "recommend", "suggest", "link", "buy", "product", "shop", "send me",
    "shampoo", "conditioner", "serum", "oil", "mask", "spray", "cleanser",
    "sunscreen", "moisturizer", "cream", "lotion", "gel"
}


def extract_product_intent(text: str) -> Optional[Dict[str, str]]:
    """
    Returns {"need_product": True, "query": <user text>} when the message
    looks like a product request; otherwise returns None.
    """
    if not text:
        return None
    t = text.lower()
    if any(k in t for k in PRODUCT_TRIGGERS):
        return {"need_product": True, "query": text.strip()}
    return None


def _is_specific_product_intent(intent_data, user_text: str) -> bool:
    """Return True for messages that clearly ask for product recs/dupes/cheaper alternatives."""
    try:
        t = (user_text or "").lower()
        if any(k in t for k in (
            "dupe", "dupes", "similar to", "like ", "alternative",
            "cheaper", "less expensive", "budget", "instead of"
        )):
            return True

        if not isinstance(intent_data, dict):
            return False

        # Obvious signals from the extractor
        if intent_data.get("intent") in {"find_products", "buy", "compare"}:
            return True
        q = (intent_data.get("query") or "").lower()
        if any(k in q for k in ("dupe", "similar", "cheaper", "less expensive", "alternative")):
            return True

        # Categories we treat as product-y by default
        if intent_data.get("category") in {"skincare", "makeup", "hair", "fragrance", "supplements"}:
            return True

        return False
    except Exception:
        return False


# === MASTER PERSONA & PLAYBOOK ===
BASE_PERSONA = """You are Schizo Bestie — the user’s emotionally fluent, pop-culture–savvy,
clairvoyant gay best friend with god-tier taste and zero tolerance for mediocrity.

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
  “daily adventures,” "Even Beyoncé has flop days," or anything similar that sounds performative or clichéd.

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
- Always include the Gumroad quiz sign-up link when pitching VIP:  
  https://schizobestie.gumroad.com/l/gexqp  
- Prompt Packs: “$7 each or 3 for $20. Think of them as your glow-up cheat codes — feed me these, and I’ll feed you clarity, savage truths, and plans that slap harder than espresso.”  
- Always include this Gumroad link when suggesting Prompt Packs:  
  https://schizobestie.gumroad.com/  

⚡ SASSY COMFORT TEMPLATE BANK  
- “Okay, slump day? Cute. Your comeback will be louder.”  
- “You’re not spiraling, you’re leveling up off-screen.”  
- “You being exhausted? That’s just the universe begging you to rest like the icon you are.”  

⚡ PRODUCT ONE-LINER TEMPLATE BANK  
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
                sqltext(
                    """
                    SELECT
                        coalesce(persona, '') AS persona,
                        coalesce(bestie_name, '') AS bestie_name,
                        coalesce(sizes::text, '') AS sizes,
                        coalesce(brands::text, '') AS brands,
                        coalesce(budget_range, '') AS budget,
                        coalesce(sensitivities::text, '') AS sensitivities,
                        coalesce(memory_notes, '') AS memory_notes
                    FROM user_profiles
                    WHERE user_id = :uid
                    """
                ),
                {"uid": user_id},
            ).first()

        quiz_data = {
            "Sizes": row[2] if row else "",
            "Favorite Brands": row[3] if row else "",
            "Budget Range": row[4] if row else "",
            "Topics to Avoid": row[5] if row else "",
            "Emotional Notes": row[6] if row else "",
        }

        quiz_profile = ""
        for k, v in quiz_data.items():
            if v:
                quiz_profile += f"{k}: {v}\n"

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
        f"{new_name}? Bold choice. Let’s make it fashion.",
    ]
    return random.choice(options)


def generate_reply(
    user_text: str,
    product_candidates: Optional[List[Dict]] = None,
    user_id: Optional[int] = None,
    system_prompt: Optional[str] = None,
    context: Optional[Dict] = None,
) -> str:
    """Generate Bestie’s reply with Geniuslink conversion + personality & loyalty rules."""

    persona_text, bestie_name = _fetch_persona_and_name(user_id)
    user_text = str(user_text or "")
    context = context or {}
    product_candidates = product_candidates or []

    # Convert all product URLs to Geniuslinks
    safe_products: List[Dict] = []
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
                final_url = geni_url
                is_monetized = True
            else:
                final_url = raw_url
                is_monetized = False

            safe_products.append(
                {
                    "name": str(p.get("name") or ""),
                    "category": str(p.get("category") or ""),
                    "url": final_url,
                    "review": str(p.get("review") or ""),
                    "monetized": is_monetized,
                }
            )

    # If no safe monetized products, fallback to showing the raw links (non-Geniuslink)
    if not safe_products and product_candidates:
        logger.info("[AI][Products] No monetized products. Falling back to raw URLs.")
        for p in product_candidates[:3]:
            raw_url = str(p.get("url") or "")
            safe_products.append(
                {
                    "name": str(p.get("name") or ""),
                    "category": str(p.get("category") or ""),
                    "url": raw_url,
                    "review": str(p.get("review") or ""),
                    "monetized": False,
                }
            )

    # Fallback if no OpenAI client
    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        if safe_products:
            p = safe_products[0]
            return f"Here’s your glow-up starter: {p['name']} ({p['category']})\n{p['url']}"
        return f"{user_text[:30]}… babe, I’ll hold space and hype you up — we’ll find the perfect rec soon 💅"

    # Build context summary
    context_summary = ""
    if context:
        for k, v in context.items():
            context_summary += f"{k}: {v}\n"

    # Build product block
    product_context = ""
    if safe_products:
        product_context = "\nHere are product candidates (already Geniuslink-converted):\n"
        for p in safe_products:
            product_context += (
                f"- {p['name']} (Category: {p['category']}) | {p['url']} | Review: {p['review']}\n"
            )

        # Pull user quiz data if available
    quiz_profile = ""
    try:
        with db.session() as s:
            row = s.execute(
                sqltext("""
                    SELECT profile_json
                    FROM user_quiz_profiles
                    WHERE user_id = :uid
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                {"uid": user_id},
            ).first()
        if row and row[0]:
            quiz_profile = (row[0] or "").strip()
    except Exception as e:
        logger.warning("[AI][Quiz] Failed to fetch quiz profile for user_id={}: {}", user_id, e)
        quiz_profile = ""

    # Build the system prompt (persona + optional quiz profile)
    system_content = system_prompt.strip() if system_prompt else persona_text
    if quiz_profile:
        system_content += f"\n\nUser Quiz Profile:\n{quiz_profile}"

    # Build user-facing content for the model
    user_content = f"""User said: {user_text}
    {context_summary}{product_context}
    Your job:
    - If venting/tired/sad → comfort + sass templates, no products.
    - If explicitly asked for products → recommend max 3 with Geniuslink URLs.
    - Product blurbs must use PRODUCT ONE-LINER TEMPLATE BANK.
    - Pitch VIP trial ($0 → $7 → $17) and Prompt Packs organically, never pushy.
    - Always comfort first, sass second, product last.
    - If promoting VIP, always include signup link: https://schizobestie.gumroad.com/l/gexqp
    - If promoting a specific Prompt Pack, always include its exact Gumroad URL based on this lookup:
    Example: 'Confidence Cleanse' → https://240026861589.gumroad.com/l/ymrag
    - Use the exact name to match the correct link. Never use the generic Gumroad shop link.
    - Never ignore or sidestep user questions. Even if vague or strange, give a thoughtful answer.
    """

    # --- Ask qualifying questions only if the ask is genuinely vague ---
    import re, random

    # important: no "recommend" here; we only catch truly vague language
    _VAGUE_REGEX = re.compile(
        r"\b(something|anything|idk|need help|what should i|ideas?|suggestions?|looking for)\b",
        re.IGNORECASE,
    )

    # Decide if the message is already specific (dupes/alternatives/price/etc.)
    try:
        intent_for_clarify = extract_product_intent(user_text)
    except Exception:
        intent_for_clarify = None

    is_specific = _is_specific_product_intent(intent_for_clarify, user_text)

    # --- Ask clarifying questions only if the message is *truly* vague (e.g., "idk") ---
    should_clarify = (
        not safe_products
        and not is_specific
        and len(user_text.strip()) < 25
        and _VAGUE_REGEX.search(user_text or "")
    )

    if should_clarify:
        logger.info("[AI][Clarify] Genuinely vague input — sending clarifier.")
        CLARIFY_LINES = [
            "Tell me the lane: skincare, style, pep talk, or full vibe reset?",
            "Product recs, style advice, or an emotional tune-up — what are we doing?",
            "Pick your flavor: skincare, fashion, feelings, or a plan I can boss you through.",
        ]
        return random.choice(CLARIFY_LINES)


    logger.info("[AI][Prompt] System:\n{}\n\nUser:\n{}", system_content, user_content)

    # --- Call OpenAI
    logger.info("[AI][Sending to GPT]\nSystem Prompt:\n{}\n\nUser Message:\n{}", system_content, user_content)
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
        return text_out

    except Exception as e:
        logger.exception("💥 [AI][Generate] OpenAI error: {}", e)
        import traceback
        logger.error("⚠️ GPT ERROR: {}", e)
        traceback.print_exc()

        if safe_products:
            p = safe_products[0]
            return f"Here’s your glow-up starter: {p['name']} ({p['category']})\n{p['url']}"
        else:
            return "Babe, I glitched — but I’ll be back with the vibe you deserve 💅"

def describe_image(image_url: str) -> str:
    """Use GPT-4o to analyze an image and return a stylish response."""
    if not image_url:
        return "Babe, I can’t analyze an empty link. Try again with an image. 😅"

    if CLIENT is None or not os.getenv("OPENAI_API_KEY"):
        return "Babe, I can't analyze images right now — something's offline. 😩"

    try:
        logger.info("[AI][Image] 📸 Analyzing image at: {}", image_url)
        response = CLIENT.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a glam, dry, emotionally fluent best friend who knows fashion, "
                        "dogs, vibes, memes, and general culture. Look at this image and describe "
                        "what you see in a sassy but intelligent tone."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe the image in 3–6 punchy sentences."},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                    ],
                },
            ],
            temperature=0.8,
            max_tokens=400,
        )
        result = (response.choices[0].message.content or "").strip()
        return result
    except Exception as e:
        logger.exception("💥 [AI][Image] GPT-4o image error: {}", e)
        return "I tried to look but something glitched. Re-upload or try again, babe."


def transcribe_and_respond(audio_url: str, user_id: Optional[int] = None) -> str:
    """Transcribe a voice note and respond as Bestie."""
    import requests

    try:
        logger.info("[AI][Voice] 🎙️ Downloading audio from {}", audio_url)
        resp = requests.get(audio_url, timeout=60)
        resp.raise_for_status()

        tmp_path = "/tmp/voice_input.mp3"
        with open(tmp_path, "wb") as f:
            f.write(resp.content)

        # Transcribe using Whisper
        with open(tmp_path, "rb") as audio_file:
            transcribed = CLIENT.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        transcript = (getattr(transcribed, "text", "") or "").strip()
        logger.info("[AI][Voice] 📝 Transcribed text: {}", transcript)

        # Send to generate_reply as if user typed it
        return generate_reply(user_text=transcript, user_id=user_id)

    except Exception as e:
        logger.exception("💥 [AI][Voice] Error transcribing/responding: {}", e)
        return "Babe, I couldn't hear that clearly — mind sending it again as a text or re-recording?"


# 👇 FULLY OUTSIDE generate_reply()
def rewrite_if_cringe(original_text: str) -> str:
    """Rewrite if GPT used banned language or failed tone check."""
    boring_flags = [
        "as an ai",
        "i am just a",
        "you are not alone",
        "i understand you are feeling",
        "beyoncé has flop days",
        "you’re still headlining",
    ]
    text_lc = (original_text or "").lower()

    if any(flag in text_lc for flag in boring_flags):
        try:
            response = CLIENT.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Rewrite this in the voice of a dry, intuitive, punchy best friend. "
                            "No pop-star metaphors. No robotic filler. Make it fierce, useful, "
                            "and emotionally intelligent."
                        ),
                    },
                    {"role": "user", "content": original_text},
                ],
                temperature=0.8,
                max_tokens=300,
            )
            new_text = (response.choices[0].message.content or "").strip()
            return new_text
        except Exception as e:
            logger.warning("[AI][Rewrite] Cringe rewrite failed: {}", e)

    return original_text
