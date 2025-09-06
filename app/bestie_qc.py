# app/bestie_qc.py
"""
Bestie QC: lightweight rubric + fixer so replies feel like Gary,
stay helpful, and keep our monetization + format rules intact.

- Scores replies on length, banned phrases, emoji count, numbering, VIP mentions.
- For product replies, ensures numbered format (1..3) is present.
- If something is off, we either (a) nudge with heuristics or (b) ask the model to rewrite.
- Never adds sales CTAs. Workers handle those.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Any, Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
QC_MAX_CHARS = int(os.getenv("QC_MAX_CHARS", "480"))  # NEW: configurable max length

BANNED = {
    "as an ai", "i am just a", "you are not alone", "i understand you are feeling",
    "say goodbye to", "hello to", "beauty arsenal", "sun-kissed glow",
    "vacation in a bottle", "spa day in your pocket", "main character in every room",
    "begging for a glow-up", "strutting like you just stepped off a yacht",
}

# NEW: tolerate colon or dash after bold name (e.g., "1. **Name**: benefit" or "1. **Name** - benefit")
NUM_LINE = re.compile(r"(?mi)^\s*\d+\.\s+\*\*(.+?)\*\*(?:[:\-–—])?\s")

def _count_emojis(s: str) -> int:
    # Rough emoji count
    return len(re.findall(r"[\U0001F300-\U0001FAFF]", s or ""))

def evaluate_reply(user_text: str, reply: str, *, has_products: bool) -> Dict[str, Any]:
    text = (reply or "").strip()
    lc = text.lower()

    issues = []
    hints  = []

    # Length discipline (workers split/trim later; QC guards here)
    length_ok = len(text) <= QC_MAX_CHARS
    if not length_ok:
        issues.append("too_long")
        hints.append(f"Trim to <= {QC_MAX_CHARS} chars.")

    # Banned stock phrasing
    banned_hits = [p for p in BANNED if p in lc]
    if banned_hits:
        issues.append("banned_phrase")
        hints.append("Remove cringe stock phrases.")

    # Em dashes
    if "—" in text or "–" in text:
        issues.append("emdash")
        hints.append("Replace em/en dashes with commas or periods.")

    # Numbered list for products
    has_numbered = bool(NUM_LINE.search(text))
    if has_products and not has_numbered:
        issues.append("missing_numbered")
        hints.append("Format products as numbered list with bold names.")

    # VIP duplication check (workers will append when relevant)
    if "gumroad.com" in lc or "vip" in lc:
        # Do not call it an issue, just a flag for fix step to remove ad copy
        hints.append("Remove VIP pitch here. Workers handle CTAs.")

    # Too many emojis (house style 0-3, ideally 1)
    if _count_emojis(text) > 3:
        issues.append("too_many_emoji")
        hints.append("Use max 3 emojis.")

    needs_fix = bool(issues)
    guidance  = "; ".join(hints) if hints else ""

    return {
        "needs_fix": needs_fix,
        "issues": issues,
        "guidance": guidance,
        "has_numbered": has_numbered,
        "length_ok": length_ok,
    }

def _heuristic_cleanup(reply: str, *, remove_vip: bool) -> str:
    txt = (reply or "").replace("—", "-").replace("–", "-")
    for p in list(BANNED):
        txt = re.sub(re.escape(p), "", txt, flags=re.I)
    # Remove inline VIP mention if present
    if remove_vip:
        txt = re.sub(r"(?i)\bvip\b.*", "", txt)
        txt = re.sub(r"https?://\S*gumroad\S*", "", txt)
    # Condense whitespace
    txt = re.sub(r"[ \t]+\n", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()

def upgrade_reply(user_text: str, reply: str, report: Dict[str, Any]) -> str:
    """
    Try a heuristic cleanup first. If problems remain and OpenAI is available,
    ask for a tight rewrite that preserves links and our product numbering.
    """
    remove_vip = "gumroad.com" in (reply or "").lower() or " vip" in (reply or "").lower()
    cleaned = _heuristic_cleanup(reply, remove_vip=remove_vip)

    # Re-evaluate after cleanup
    again = evaluate_reply(user_text, cleaned, has_products=report.get("has_products", False))
    if not again["needs_fix"]:
        return cleaned

    # If model not available, return the cleaned version
    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return cleaned

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    guidance = (
        f"Rewrite for SMS. Keep <= {QC_MAX_CHARS} chars. No sales CTAs. "
        "If products present, format as numbered list:\n"
        "1. **Name**: one-liner. URL\n"
        "Do not remove or alter any URLs. Avoid cringe stock phrases and em dashes."
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.7,
            max_tokens=360,
            messages=[
                {"role": "system", "content": guidance},
                {"role": "user", "content": cleaned},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or cleaned
    except Exception:
        return cleaned
