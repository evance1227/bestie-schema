import re

def to_plain_sms(text: str) -> str:
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 — \2", text)
    return text
