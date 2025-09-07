# app/safety.py
# -----------------------------------------------------------------------------
# Badass-safe guardrails for Bestie.
# - Warm, direct, adult. Swearing allowed. Relationship and intimacy talk allowed.
# - Blocks only what creates legal or safety exposure.
# - Returns: a safety reply (str) when blocking/redirecting, otherwise None.
# - Keep this first in your pipeline before persona rendering.
# -----------------------------------------------------------------------------

from __future__ import annotations
import re
from enum import Enum, auto
from typing import Optional, Set, Dict, List

# =========================
# Risk taxonomy
# =========================
class Risk(Enum):
    SELF_HARM = auto()
    HARM_OTHERS = auto()
    ILLEGAL = auto()
    WEAPONS = auto()
    EATING_DISORDER = auto()
    MED_DOSING = auto()
    DRUG_MISUSE = auto()
    MINOR_SEX = auto()          # any sexual content involving minors
    ADULT_EXPLICIT_HOWTO = auto()  # explicit sex "how-to" or pornographic instruction
    HATE = auto()
    DEFAMATION = auto()
    PRIVACY = auto()
    FINANCE_PROMISE = auto()
    LEGAL_ADVICE = auto()
    DANGEROUS_DANGER_DDIY = auto() # explosives, high voltage, toxic mixes, etc.
    STALKING_SURVEILLANCE = auto()
    UNDERAGE_DISCLOSED = auto()    # user self-reports <18
    MED_DIAGNOSIS = auto()         # diagnosis requests without clinician
    EXTREMISM = auto()

# =========================
# Helpers
# =========================
_AGE_DECL = re.compile(r"\b(i\s*(am|'m)\s*(\d{1,2}))\b", re.I)

def _detect_underage(text: str) -> bool:
    m = _AGE_DECL.search(text)
    if not m:
        return False
    try:
        age = int(m.group(3))
        return age < 18
    except Exception:
        return False

def _norm(text: str) -> str:
    return text.lower()

# =========================
# Pattern library (expandable)
# Keep lowercase where possible.
# =========================
PATS: Dict[Risk, List[str]] = {
    Risk.SELF_HARM: [
        r"\bkill myself\b", r"\bsuicid", r"\bunalive\b", r"\bkms\b",
        r"\bexit\s*bag\b", r"\bhelium\s*hood\b", r"\bnitrogen\b.*\bdeath\b",
        r"\bhow to die\b", r"\bbeautiful suicide\b", r"\b(end my life|no reason to live)\b",
    ],
    Risk.HARM_OTHERS: [
        r"\bkill (him|her|them)\b", r"\bhow to (hurt|poison)\b", r"\bmake (them|someone) suffer\b",
    ],
    Risk.WEAPONS: [
        r"\bghost\s*gun\b", r"\bauto\s*sear\b", r"\bconvert\b.*\b(full|auto)\b",
        r"\b(make|build).*\b(bomb|explosive|silencer)\b",
    ],
    Risk.ILLEGAL: [
        r"\bbreak\s*in(to)?\b", r"\bshoplift\b", r"\bsteal\b", r"\bfake\s*id\b",
        r"\bhack(ing)?\b", r"\bdoxx?\b", r"\bkey\s*logger\b", r"\bbank\s*fraud\b",
    ],
    Risk.EATING_DISORDER: [
        r"\bthinspo\b", r"\bpro-?ana\b", r"\bpurge\b", r"\blaxative.*weight\b",
        r"\bhow to avoid eating\b", r"\bstarve\b", r"\bchew\s*and\s*spit\b",
    ],
    Risk.MED_DOSING: [
        r"\b\d+\s*(mg|mcg|iu)\b.*\b(take|dose|stack)\b", r"\bhow much\b.*\b(prescription|med|pill|injection)\b",
        r"\bcombine\b.*\b(ssri|maoi|benzo|steroid|tirzepatide|semaglutide)\b.*\b(dose|microdose)\b",
    ],
    Risk.DRUG_MISUSE: [
        r"\bcook\s*meth\b", r"\bfreebase\b", r"\bfentanyl\b.*\bdose\b",
        r"\b(xanax|benzo)\b.*\b(alcohol|booze)\b", r"\bhow to get high\b.*\b(pills|syrup)\b",
    ],
    Risk.MINOR_SEX: [
        r"\b(13|14|15|16|17)\b.*\bsex\b", r"\bminor\b.*\bsex\b", r"\bgroom(ing)?\b",
        r"\bunderage\b.*\b(sex|nudes|pics)\b",
    ],
    # Only block explicit porn-style or step-by-step sexual technique.
    # Adult intimacy, consent, communication, and safety are allowed.
    Risk.ADULT_EXPLICIT_HOWTO: [
        r"\bhow to\b.*\b(perform|do)\b.*\b(anal|oral|blowjob|deepthroat|fisting)\b",
        r"\bstep[-\s]?by[-\s]?step\b.*\b(sex|anal|oral)\b",
        r"\bbest way to\b.*\bmake (her|him|them) (orgasm|squirt|cum)\b",
        r"\bdiagram\b.*\bsex position\b", r"\bunsafe\b.*\bchoking\b",
    ],
    Risk.HATE: [
        r"\b(gas the|lynch|exterminate)\b", r"\b(?:slur|kike|wetback|chink|fag|tranny)\b",
    ],
    Risk.DEFAMATION: [
        r"\bis it true\b.*\b(criminal|rapist|fraud)\b.*\b(@|\b[a-z]+ [a-z]+)\b",
        r"\bexpose\b.*\bscam\b.*\b(person|private)\b",
    ],
    Risk.PRIVACY: [
        r"\baddress|phone|email|ssn|social security|location\b.*\b(find|get|trace|track)\b",
        r"\b(ip|mac)\b.*\blookup\b",
    ],
    Risk.FINANCE_PROMISE: [
        r"\bguarantee(d|)\b.*\breturns?\b", r"\binsider\b.*\btip\b", r"\bsurefire\b.*\bprofit\b",
    ],
    Risk.LEGAL_ADVICE: [
        r"\bbeat the case\b", r"\bhow to avoid charges\b", r"\bhide evidence\b",
    ],
    Risk.DANGEROUS_DANGER_DDIY: [
        r"\bhigh voltage\b.*\bhow to\b", r"\bmix\b.*\bbleach\b.*\bammonia\b",
        r"\bhandle\b.*\bmercury|cyanide|nitro\b",
    ],
    Risk.STALKING_SURVEILLANCE: [
        r"\bairtag\b.*\btrack\b.*\b(person|partner)\b", r"\bhack\b.*\b(phone|icloud)\b",
        r"\bspyware\b|\bstalkerware\b", r"\bhidden camera\b.*\bsetup\b",
    ],
    Risk.MED_DIAGNOSIS: [
        r"\bdo i have\b.*\bdiagnose\b", r"\bwhat illness is\b", r"\bself[-\s]?diagnos",
    ],
    Risk.EXTREMISM: [
        r"\bjoin\b.*\b(isis|al[-\s]?qaeda)\b", r"\bcreate\b.*\bmanifesto\b.*\battack\b",
    ],
}

# =========================
# Replies
# Voice: kind, concise, no lectures.
# =========================
def _crisis_reply(user_country: str) -> str:
    pre = "I’m really glad you told me. You matter and I want you safe.\n"
    if user_country.upper() == "US":
        return pre + "If you’re in immediate danger, call 911 now. You can call or text 988 for the Suicide and Crisis Lifeline. I’m here with you."
    return pre + "If you’re in immediate danger, call your local emergency number now. You deserve immediate help, and I’m here with you."

REFUSALS: Dict[Risk, str] = {
    Risk.HARM_OTHERS: "I can’t help with hurting someone. If you’re angry or overwhelmed, I can help you cool off and choose a safer next move.",
    Risk.WEAPONS: "I can’t help with weapons or conversion instructions. If safety is the concern, I can share nonviolent security tips.",
    Risk.ILLEGAL: "I can’t help with illegal activity. If you want legal alternatives or education, I’m in.",
    Risk.EATING_DISORDER: "I can’t help with harming your body. If food and feelings are loud, I can offer kind support and resources.",
    Risk.DRUG_MISUSE: "I can’t help with illicit drug use or dangerous combos. I can share general harm-reduction resources or suggest talking with a professional.",
    Risk.MED_DOSING: "I can’t give dosing or personalized medical instructions. I can explain general concepts and help you prep questions for a clinician.",
    Risk.MINOR_SEX: "I can’t engage with sexual topics involving anyone under 18. If you’re in an unsafe situation, contact a trusted adult or your local emergency number.",
    Risk.ADULT_EXPLICIT_HOWTO: "I won’t give explicit sexual instructions. I can help with consent, communication, safety, prep, aftercare, and how to talk to a partner.",
    Risk.HATE: "I won’t produce hateful or violent content. Happy to help with respectful discussion or learning.",
    Risk.DEFAMATION: "I can’t make or spread accusations about private people. I can explain how to verify sources or discuss the topic generally.",
    Risk.PRIVACY: "I can’t share or dig up private personal data. I can help you find public, consented info instead.",
    Risk.FINANCE_PROMISE: "I can’t promise outcomes or share insider info. I can walk you through risk and the math so you can decide.",
    Risk.LEGAL_ADVICE: "I’m not a lawyer and I can’t advise on evading law enforcement. I can help you prep smart questions for a licensed attorney.",
    Risk.DANGEROUS_DANGER_DDIY: "I can’t walk you through hazardous procedures. I can suggest safer alternatives or pro services.",
    Risk.STALKING_SURVEILLANCE: "I can’t help with spying or tracking people. If you’re concerned about safety, I can discuss protective options.",
    Risk.MED_DIAGNOSIS: "I can’t diagnose or replace a clinician. I can explain possibilities and help you plan what to ask a professional.",
    Risk.EXTREMISM: "I won’t assist with extremist activity. I can discuss history or safety topics at a high level if that helps.",
}

# Minor-specific redirect when the user discloses they’re under 18
UNDERAGE_REDIRECT = (
    "Thanks for trusting me. Because you said you’re under 18, I keep things age-appropriate. "
    "If you’re in danger or feel unsafe, contact a trusted adult or your local emergency number right now. "
    "I’m here to help with safe topics, school, friendships, and everyday support."
)

# Allowed intimacy guidance (adult, non-explicit)
INTIMACY_SAFE_HELP = (
    "Adult intimacy is fair game. I avoid explicit step-by-step sexual technique, "
    "but I can help you with consent scripts, boundaries, communication, safety, preparation, and aftercare. "
    "Tell me what you want help with and I’ll keep it respectful and useful."
)

# =========================
# Scanner
# =========================
def _scan(text: str) -> Set[Risk]:
    t = _norm(text)
    hits: Set[Risk] = set()
    # age disclosure
    if _detect_underage(t):
        hits.add(Risk.UNDERAGE_DISCLOSED)
    for risk, pats in PATS.items():
        if any(re.search(p, t) for p in pats):
            hits.add(risk)
    return hits

# =========================
# Public API
# =========================
def safety_guard(text: str, user_country: str = "US", user_is_minor: Optional[bool] = None) -> Optional[str]:
    """
    Returns a safety message if we must block or redirect. Otherwise returns None.
    - Swearing is fine.
    - Relationship advice is allowed.
    - Adult intimacy talk is allowed at a high level (consent, communication, safety, aftercare).
    """
    risks = _scan(text)

    # If caller knows age, honor it
    minor = user_is_minor if user_is_minor is not None else (Risk.UNDERAGE_DISCLOSED in risks)

    # 1) Self-harm gets immediate crisis support
    if Risk.SELF_HARM in risks:
        return _crisis_reply(user_country)

    # 2) Sexual content with minors or user is a minor asking for sexual content
    if minor and (Risk.MINOR_SEX in risks or "sex" in _norm(text)):
        return UNDERAGE_REDIRECT

    # 3) Highest-severity illegal/violent buckets
    for r in [
        Risk.HARM_OTHERS, Risk.WEAPONS, Risk.EXTREMISM, Risk.ILLEGAL,
        Risk.STALKING_SURVEILLANCE, Risk.DANGEROUS_DANGER_DDIY
    ]:
        if r in risks:
            return REFUSALS[r]

    # 4) Health and substance guardrails
    for r in [Risk.MED_DOSING, Risk.MED_DIAGNOSIS, Risk.DRUG_MISUSE, Risk.EATING_DISORDER]:
        if r in risks:
            return REFUSALS[r]

    # 5) Speech, privacy, finance, legal
    for r in [Risk.HATE, Risk.DEFAMATION, Risk.PRIVACY, Risk.FINANCE_PROMISE, Risk.LEGAL_ADVICE]:
        if r in risks:
            return REFUSALS[r]

    # 6) Adult explicit sexual how-to: redirect to safe intimacy help
    if Risk.ADULT_EXPLICIT_HOWTO in risks:
        return INTIMACY_SAFE_HELP

    # Otherwise, we’re good. Proceed.
    return None

# Optional: expose a scanner for analytics or tests
def safety_scan_categories(text: str) -> Set[str]:
    """Return matched risk categories by name for logging or CI tests."""
    return {r.name for r in _scan(text)}
