# -*- coding: utf-8 -*-
"""
Bestie One-Liner Vault
----------------------
This module stores monetizable one-liners (quip + tag) and helpers to render them
with affiliate links. Usage patterns:
- tag == "rec": Recommend product directly with CTA/link.
- tag == "anti_rec_to_rec": Roast the bad option, then pivot to a better product with CTA/link.

File layout:
1) Header (you are here)
2) Four data slices that define/extend BESTIE_ONELINERS
3) Helper functions (rendering + selection)
"""

from __future__ import annotations

from typing import TypedDict, Literal, List, Dict, Any, Optional
import random
import html

# -------------------------------
# Types & constants
# -------------------------------

Category = Literal["snack", "skincare", "haircare", "fashion", "home"]
Tag = Literal["rec", "anti_rec_to_rec"]

class OneLiner(TypedDict):
    id: int
    category: Category
    line: str
    tag: Tag

# Public API from this module (BESTIE_ONELINERS is defined by your slices below)
__all__ = [
    "OneLiner",
    "Category",
    "Tag",
    "BESTIE_ONELINERS",
    "filter_oneliners",
    "pick_oneliner",
    "render_rec",
    "render_anti_rec_to_rec",
    "render_oneliner_with_link",
    "esc",
]

# Default “why” blurbs used by helpers if no reason is provided.
_DEFAULT_REASONS_BY_CATEGORY: Dict[str, str] = {
    "skincare": "so your pores stay calm and breakout-free",
    "haircare": "to protect your length and keep frizz in check",
    "fashion": "so it actually looks and feels luxe",
    "home": "so it lasts and doesn’t feel cheap",
    "snack": "because taste buds deserve better",
}

# -------------------------------
# Data slices go below this line:
# (Paste Slice 1, Slice 2, Slice 3, Slice 4)
# -------------------------------


BESTIE_ONELINERS = [
    {
        "id": 1,
        "category": "snack",
        "line": "Starburst (unwrapped packs) – Because fumbling with wrappers while baked is a crime against humanity.",
        "tag": "rec",
    },
    {
        "id": 2,
        "category": "snack",
        "line": "Nerds Rope – Chaotic chewy-crunchy perfection.",
        "tag": "rec",
    },
    {
        "id": 3,
        "category": "snack",
        "line": "Peanut M&M’s – Built different. Like a protein shake in candy form.",
        "tag": "rec",
    },
    {
        "id": 5,
        "category": "snack",
        "line": "Hot Cheetos – The pain makes them taste better.",
        "tag": "rec",
    },
    {
        "id": 10,
        "category": "snack",
        "line": "Oreos – Therapy session between two cookies.",
        "tag": "rec",
    },
    {
        "id": 22,
        "category": "snack",
        "line": "Takis – The snack equivalent of yelling in Spanish.",
        "tag": "rec",
    },
    {
        "id": 23,
        "category": "skincare",
        "line": "Retinol cream – The love child of hope and peeling.",
        "tag": "rec",
    },
    {
        "id": 25,
        "category": "haircare",
        "line": "Dry shampoo – The patron saint of “I’ll shower tomorrow.”",
        "tag": "rec",
    },
    {
        "id": 37,
        "category": "haircare",
        "line": "Kerastase mask – A religious experience disguised as conditioner.",
        "tag": "rec",
    },
    {
        "id": 49,
        "category": "haircare",
        "line": "Headbands – Crown or curse, depending on your forehead.",
        "tag": "rec",
    },
    {
        "id": 51,
        "category": "haircare",
        "line": "Box dye – The ex-boyfriend of hair decisions: cheap, messy, regretful.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 53,
        "category": "haircare",
        "line": "Banana clip – The mullet of hair accessories.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 57,
        "category": "skincare",
        "line": "Shea butter in acne products – Like throwing a pizza party on your pores.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 58,
        "category": "skincare",
        "line": "Coconut oil in face creams – Breakout bait dressed up as ‘natural skincare.’",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 59,
        "category": "skincare",
        "line": "Alcohol-heavy toners – Basically paint thinner in a skincare bottle.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 61,
        "category": "skincare",
        "line": "Apricot scrubs – Sandpaper cosplay.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 62,
        "category": "skincare",
        "line": "SPF under 30 – That’s not protection, that’s a suggestion.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 67,
        "category": "haircare",
        "line": "Sulfate shampoos – Dish soap for your scalp.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 69,
        "category": "haircare",
        "line": "Drugstore mousse – Crunchy helmet chic.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 74,
        "category": "haircare",
        "line": "Cheap flat irons – Fried hair for $29.99.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 76,
        "category": "fashion",
        "line": "Cheap leggings – Transparent betrayal.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 79,
        "category": "home",
        "line": "Flimsy coffee makers – Brown water machines.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 80,
        "category": "home",
        "line": "Drugstore candles – Scented disappointment.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 82,
        "category": "home",
        "line": "Ugly desk chairs – Chiropractor sponsorship waiting to happen.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 83,
        "category": "home",
        "line": "Random Amazon vacuums – Dust bunnies unionize.",
        "tag": "anti_rec_to_rec",
    }
]
BESTIE_ONELINERS += [
    {
        "id": 85,
        "category": "home",
        "line": "Plastic Tupperware graveyard – Sad lids, sad life.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 6,
        "category": "snack",
        "line": "Gummy bears – The emotional support animals of candy.",
        "tag": "rec",
    },
    {
        "id": 7,
        "category": "snack",
        "line": "Peach rings – Childhood nostalgia with a side of cavities.",
        "tag": "rec",
    },
    {
        "id": 8,
        "category": "snack",
        "line": "Twizzlers – Snack + straw = efficiency queen.",
        "tag": "rec",
    },
    {
        "id": 9,
        "category": "snack",
        "line": "Chocolate-covered pretzels – Sweet + salty = relationship goals.",
        "tag": "rec",
    },
    {
        "id": 11,
        "category": "snack",
        "line": "Ben & Jerry’s pint – A solo date that always shows up.",
        "tag": "rec",
    },
    {
        "id": 12,
        "category": "snack",
        "line": "Rice Krispies treats – Nostalgia that slaps harder high.",
        "tag": "rec",
    },
    {
        "id": 13,
        "category": "snack",
        "line": "Sour Skittles – Tiny rainbow grenades.",
        "tag": "rec",
    },
    {
        "id": 14,
        "category": "snack",
        "line": "Pop-Tarts – Breakfast or midnight chaos, your choice.",
        "tag": "rec",
    },
    {
        "id": 15,
        "category": "snack",
        "line": "Doritos – Finger dust = badge of honor.",
        "tag": "rec",
    },
    {
        "id": 16,
        "category": "snack",
        "line": "Funyuns – Onion rings for people who’ve given up on dignity.",
        "tag": "rec",
    },
    {
        "id": 17,
        "category": "snack",
        "line": "Hostess cupcakes – Frosting hats covering existential dread.",
        "tag": "rec",
    },
    {
        "id": 18,
        "category": "snack",
        "line": "Cosmic Brownies – Nostalgia and diabetes in one bite.",
        "tag": "rec",
    },
    {
        "id": 19,
        "category": "snack",
        "line": "Beef jerky – The leather jacket of snacks.",
        "tag": "rec",
    },
    {
        "id": 20,
        "category": "snack",
        "line": "Slim Jims – Gas station meat straws.",
        "tag": "rec",
    },
    {
        "id": 21,
        "category": "snack",
        "line": "Cup Noodles – Broke college chic forever.",
        "tag": "rec",
    },
    {
        "id": 27,
        "category": "skincare",
        "line": "Clay mask – Feels like a spa, looks like swamp cosplay.",
        "tag": "rec",
    },
    {
        "id": 28,
        "category": "skincare",
        "line": "Sheet masks – For when you want to scare the dog and fix your skin.",
        "tag": "rec",
    },
    {
        "id": 29,
        "category": "skincare",
        "line": "Eye patches – Little under-eye miracles pretending to be stickers.",
        "tag": "rec",
    },
    {
        "id": 30,
        "category": "skincare",
        "line": "Lip gloss – Sticky but worth it.",
        "tag": "rec",
    },
    {
        "id": 31,
        "category": "skincare",
        "line": "Mascara – Coffee for your lashes.",
        "tag": "rec",
    },
    {
        "id": 32,
        "category": "skincare",
        "line": "Bronzer – Sunlight in powder form.",
        "tag": "rec",
    },
    {
        "id": 33,
        "category": "skincare",
        "line": "Brow gel – Hairspray for your sassiest feature.",
        "tag": "rec",
    },
    {
        "id": 34,
        "category": "skincare",
        "line": "Charcoal mask – Looks like Batman cosplay, rips like wax.",
        "tag": "rec",
    },
    {
        "id": 35,
        "category": "skincare",
        "line": "Pore strips – Satisfaction and pain in one peel.",
        "tag": "rec",
    }
]
BESTIE_ONELINERS += [
    {
        "id": 36,
        "category": "skincare",
        "line": "Red lipstick – Warning label for men and meetings.",
        "tag": "rec",
    },
    {
        "id": 38,
        "category": "haircare",
        "line": "Olaplex No.3 – The relationship counselor for broken hair.",
        "tag": "rec",
    },
    {
        "id": 39,
        "category": "haircare",
        "line": "Blow-dry brush – DIY salon magic, plus arm workout included.",
        "tag": "rec",
    },
    {
        "id": 40,
        "category": "haircare",
        "line": "Claw clips – The comeback kid of hair accessories.",
        "tag": "rec",
    },
    {
        "id": 41,
        "category": "haircare",
        "line": "Scrunchies – Therapy, but for your ponytail.",
        "tag": "rec",
    },
    {
        "id": 42,
        "category": "haircare",
        "line": "Shower cap – The unsung hero of “I don’t feel like washing today.”",
        "tag": "rec",
    },
    {
        "id": 43,
        "category": "haircare",
        "line": "Heat protectant spray – The bodyguard your hair actually needs.",
        "tag": "rec",
    },
    {
        "id": 44,
        "category": "haircare",
        "line": "Hair mousse – The forgotten ‘90s icon making a comeback.",
        "tag": "rec",
    },
    {
        "id": 45,
        "category": "haircare",
        "line": "Hair spray – Weatherproofing for your style.",
        "tag": "rec",
    },
    {
        "id": 46,
        "category": "haircare",
        "line": "Velcro rollers – Grandma was right, volume lives here.",
        "tag": "rec",
    },
    {
        "id": 47,
        "category": "haircare",
        "line": "Detangling spray – Negotiator for knots.",
        "tag": "rec",
    },
    {
        "id": 48,
        "category": "haircare",
        "line": "Hair mask packets – Commitment issues? Try the travel size.",
        "tag": "rec",
    },
    {
        "id": 54,
        "category": "haircare",
        "line": "Crimping iron – Trauma from 2002 in appliance form.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 55,
        "category": "haircare",
        "line": "Glitter gel – Your scalp will remember this forever.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 56,
        "category": "haircare",
        "line": "Travel-size shampoo – Costs more than therapy per ounce.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 60,
        "category": "skincare",
        "line": "Fragrance-loaded serums – Smells good, feels like betrayal.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 63,
        "category": "skincare",
        "line": "Micellar water – Makeup remover that taps out early.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 64,
        "category": "skincare",
        "line": "Overly harsh acne kits – Congrats, you cleared your pimples and your dignity.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 65,
        "category": "skincare",
        "line": "Essential oils directly on skin – Aromatherapy meets chemical burn.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 66,
        "category": "skincare",
        "line": "Clay masks every night – Babe, that’s a drought, not a routine.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 68,
        "category": "haircare",
        "line": "Box dye black – Goth cosplay you’ll regret in six washes.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 70,
        "category": "haircare",
        "line": "Plastic vent brushes – Breakage machines.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 71,
        "category": "haircare",
        "line": "2-in-1 shampoo + conditioner – Red flag in a bottle.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 72,
        "category": "haircare",
        "line": "Glitter hairspray – High school dance trauma in a can.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 73,
        "category": "haircare",
        "line": "Elastic rubber bands – Snap crackle pop… of your hairline.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 75,
        "category": "haircare",
        "line": "Perm kits at home – Curl crimes against humanity.",
        "tag": "anti_rec_to_rec",
    }
]
BESTIE_ONELINERS += [
    {
        "id": 76,
        "category": "fashion",
        "line": "Cheap leggings – Transparent betrayal.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 77,
        "category": "fashion",
        "line": "Polyester blazers – Business cosplay.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 78,
        "category": "fashion",
        "line": "Fast-fashion heels – Six steps and you’re limping.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 79,
        "category": "home",
        "line": "Flimsy coffee makers – Brown water machines.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 80,
        "category": "home",
        "line": "Drugstore candles – Scented disappointment.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 81,
        "category": "home",
        "line": "Plastic wine glasses – College energy.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 82,
        "category": "home",
        "line": "Ugly desk chairs – Chiropractor sponsorship waiting to happen.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 83,
        "category": "home",
        "line": "Random Amazon vacuums – Dust bunnies unionize.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 84,
        "category": "home",
        "line": "Basic bedding sets – Hotel 6 chic.",
        "tag": "anti_rec_to_rec",
    },
    {
        "id": 85,
        "category": "home",
        "line": "Plastic Tupperware graveyard – Sad lids, sad life.",
        "tag": "anti_rec_to_rec",
    }
]
# ---------------------------------------------
# One-liner helpers for Bestie
# Paste below your BESTIE_ONELINERS slices
# ---------------------------------------------
from typing import List, Optional, Dict, Any
import random
import html

# Default “why” blurbs used if you don’t pass a reason
_DEFAULT_REASONS_BY_CATEGORY: Dict[str, str] = {
    "skincare": "so your pores stay calm and breakout-free",
    "haircare": "to protect your length and keep frizz in check",
    "fashion": "so it actually looks and feels luxe",
    "home": "so it lasts and doesn’t feel cheap",
    "snack": "because taste buds deserve better",
}

def filter_oneliners(
    *,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    include_ids: Optional[List[int]] = None,
    exclude_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """
    Return a filtered list from BESTIE_ONELINERS.
    - category: "snack" | "skincare" | "haircare" | "fashion" | "home" | None
    - tag: "rec" | "anti_rec_to_rec" | None
    - include_ids / exclude_ids: optional id filtering
    """
    items = BESTIE_ONELINERS[:]
    if category:
        items = [x for x in items if x.get("category") == category]
    if tag:
        items = [x for x in items if x.get("tag") == tag]
    if include_ids:
        only = set(include_ids)
        items = [x for x in items if x.get("id") in only]
    if exclude_ids:
        skip = set(exclude_ids)
        items = [x for x in items if x.get("id") not in skip]
    return items

def pick_oneliner(
    *,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    exclude_ids: Optional[List[int]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """
    Grab a single oneliner dict at random using optional filters.
    Returns None if no match.
    """
    rng = rng or random
    pool = filter_oneliners(category=category, tag=tag, exclude_ids=exclude_ids)
    return rng.choice(pool) if pool else None

def render_rec(
    line_obj: Dict[str, Any],
    *,
    product_name: str,
    affiliate_url: str,
    cta: str = "Grab it here",
) -> str:
    """
    Render a [REC] line with a clean CTA + link.
    """
    line = line_obj.get("line", "")
    product_name = product_name.strip()
    url = affiliate_url.strip()
    # Keep it SMS-friendly but punchy
    return (
        f"{line}\n"
        f"→ {product_name}: {cta} → {url}"
    )

def render_anti_rec_to_rec(
    line_obj: Dict[str, Any],
    *,
    alt_product_name: str,
    alt_affiliate_url: str,
    reason: Optional[str] = None,
    persona_hint: Optional[str] = None,
    cta: str = "Try this instead",
) -> str:
    """
    Render an [ANTI-REC → REC] with a savage dunk then a pivot.
    - reason: optional short why (e.g., “no pore-clogging oils”, “less heat damage”)
    - persona_hint: optional tailwind for tone (e.g., “gentle”, “extra savage”)
    """
    line = line_obj.get("line", "")
    category = line_obj.get("category", "home")
    why = reason or _DEFAULT_REASONS_BY_CATEGORY.get(category, "because it’s better, period")
    alt_name = alt_product_name.strip()
    url = alt_affiliate_url.strip()

    # A couple of persona-aware pivots (kept tight for SMS)
    pivot_openers = [
        "No babe, don’t waste it.",
        "Hard pass. Upgrade time.",
        "Skip the struggle.",
        "We’re not doing that.",
        "You deserve better.",
    ]
    opener = random.choice(pivot_openers)

    return (
        f"{line}\n"
        f"{opener} {why.capitalize()} — {cta}: {alt_name} → {url}"
    )

# Optional: a convenience “router” that decides how to render based on tag
def render_oneliner_with_link(
    *,
    category: Optional[str] = None,
    prefer_tag: Optional[str] = None,
    exclude_ids: Optional[List[int]] = None,
    rec_product_name: Optional[str] = None,
    rec_affiliate_url: Optional[str] = None,
    anti_rec_alt_name: Optional[str] = None,
    anti_rec_alt_url: Optional[str] = None,
    reason: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """
    High-level helper:
    - Picks a line by category & tag (falls back if empty).
    - Renders with the right template.
    Usage:
        render_oneliner_with_link(
            category="haircare",
            prefer_tag="anti_rec_to_rec",
            anti_rec_alt_name="Living Proof Flex Hairspray",
            anti_rec_alt_url="https://syl.link/...",
            reason="no helmet hair, flexible hold"
        )
    """
    rng = rng or random

    # Try preferred tag first, then fall back to either pool
    line = pick_oneliner(category=category, tag=prefer_tag, exclude_ids=exclude_ids, rng=rng)
    if not line:
        # fall back to any tag in that category
        line = pick_oneliner(category=category, tag=None, exclude_ids=exclude_ids, rng=rng)
    if not line:
        return None

    tag = line.get("tag")
    if tag == "rec":
        if not (rec_product_name and rec_affiliate_url):
            # If product missing, just return the quip
            return line.get("line", "")
        return render_rec(
            line,
            product_name=rec_product_name,
            affiliate_url=rec_affiliate_url,
        )

    # anti_rec_to_rec
    if not (anti_rec_alt_name and anti_rec_alt_url):
        # If alt missing, still deliver the dunk so convo flows
        return line.get("line", "")
    return render_anti_rec_to_rec(
        line,
        alt_product_name=anti_rec_alt_name,
        alt_affiliate_url=anti_rec_alt_url,
        reason=reason,
    )

# Tiny utility to safely escape text if you need HTML contexts (optional)
def esc(text: str) -> str:
    return html.escape(text, quote=True)
