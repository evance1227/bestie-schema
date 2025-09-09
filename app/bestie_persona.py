# -*- coding: utf-8 -*-
"""
Bestie Persona Library
----------------------
Pure helpers + text assets to shape tone, ban cringe, and render witty,
affiliate-ready replies. Safe to add — it does not import or mutate
any of your running code.

Usage (later, when you choose):
    from app.bestie_persona import (
        BESTIE_TONE_RULES, BAN_PHRASES, OPENERS,
        apply_banlist, pick_opener, render_holy_grail_line,
        render_numbered_products, skin_pivot
    )
"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple
import random
import re

# -----------------------------
# Core voice + guardrails
# -----------------------------
BESTIE_TONE_RULES = """
You are the Unhinged Barbie Bestie: blunt, funny, glam, emotionally fluent.
- Be specific, punchy, and useful in ~450 chars.
- Be confident; never apologetic or clinical.
- Prefer jokes that sell the upgrade.
- If only one truly good pick: say "This is THE one:" then link.
- If a product is bad for acne/sensitive hair/whatever: roast it, then pivot to a safer rec with a link.
- No therapy clichés. No corporate brochure tone. No "as an AI".
"""

BAN_PHRASES = {
    "as an ai", "language model", "in conclusion", "according to", "URL:", "I cannot", "I’m unable to"
}

OPENERS = [
    "Okay babe, quick truth bomb:",
    "Bestie verdict incoming:",
    "No fluff. Just the fix:",
    "Savage but helpful:",
    "Here’s the play:",
]

def apply_banlist(text: str) -> str:
    t = text or ""
    for p in BAN_PHRASES:
        t = re.sub(re.escape(p), "", t, flags=re.I)
    # collapse double spaces created by removals
    return re.sub(r"\s{2,}", " ", t).strip()

def pick_opener(rng: Optional[random.Random] = None) -> str:
    rng = rng or random
    return rng.choice(OPENERS)

# -----------------------------
# Skin/hair “science” pivots
# -----------------------------
# Short, brand-safe reasons Bestie can cite when pivoting.
REASONS = {
    "acne": {
        "coconut oil": "that clogs pores (breakout bait)",
        "shea butter": "heavy + comedogenic for acne-prone skin",
        "isopropyl myristate": "famously pore-clogging",
        "fragrance": "can irritate and trigger breakouts",
        "alcohol": "can strip barrier and worsen oil rebounds",
    },
    "sensitive": {
        "fragrance": "a top irritant for reactive skin",
        "alcohol": "can sting and dehydrate",
        "essential oils": "cute scent, messy irritation",
    },
    "oily": {
        "heavy oils": "too occlusive for oily skin",
        "butters": "sit on top and clog",
    },
    "dry": {
        "foaming sulfates": "strip your barrier and squeak you out",
        "strong acids": "over-exfoliate a dry barrier",
    },
    "hair": {
        "sulfates": "strip color and moisture",
        "cheap alcohols": "dry your ends to straw",
        "rubber bands": "snap city for your hairline",
    },
}

def skin_pivot(
    *,
    concern: str,                 # "acne" | "sensitive" | "oily" | "dry" | "hair"
    bad_ingredient: str,          # e.g. "shea butter"
    alt_name: str,                # e.g. "Paula's Choice 2% BHA"
    alt_url: str,                 # affiliate or SYL link (already wrapped)
    cta: str = "Try this instead",
) -> str:
    why = REASONS.get(concern, {}).get(bad_ingredient.lower(), "not ideal for your profile")
    opener = pick_opener()
    line = (
        f"{opener} if you’re {concern}-prone, {bad_ingredient} is {why}. "
        f"{cta}: {alt_name} → {alt_url}"
    )
    return apply_banlist(line)

# -----------------------------
# Product render helpers (pure)
# -----------------------------
def render_holy_grail_line(title: str, url: str) -> str:
    """
    One perfect pick: keep it iconic and short.
    Your worker already flattens links and adds closers.
    """
    line = f"This is THE one: **{title.strip()}** {url.strip()}"
    return apply_banlist(line)

def render_numbered_products(products: List[Dict], limit: int = 3) -> str:
    """
    Format up to N products as a numbered list with bold names and links.
    Each product dict: { 'name'|'title': str, 'url': str, 'review': str? }
    """
    items = []
    for i, p in enumerate(products[:limit], 1):
        name = (p.get("title") or p.get("name") or "Product").strip()
        url  = (p.get("url") or "").strip()
        blurb = (p.get("review") or "").strip()
        if blurb:
            items.append(f"{i}. **{name}** — {blurb}. {url}")
        else:
            items.append(f"{i}. **{name}** {url}")
    text = pick_opener() + "\n" + "\n".join(items)
    return apply_banlist(text)

# -----------------------------
# Personality micro-snippets (optional)
# -----------------------------
# Drop-in one-liners your bot can append for extra flavor.
MICRO_SNIPPETS = {
    "pizza_party_pores": "That’s a pizza party for your pores.",
    "lego_hair": "No one wants to look like a Lego figurine, babe.",
    "edible_confetti": "Popcorn is basically edible confetti.",
    "protein_candy": "Peanut M&M’s = the protein shake of candy.",
}

def add_micro(text: str, key: str) -> str:
    if key in MICRO_SNIPPETS:
        return apply_banlist((text or "").rstrip() + " " + MICRO_SNIPPETS[key])
    return text or ""

# -----------------------------
# Safe presets for system/instructions
# -----------------------------
def system_persona_block() -> str:
    """Return a short block you can prepend to your system prompt."""
    return BESTIE_TONE_RULES.strip()

