"""Ghana phone number handling.

Users type numbers every which way - "0244123456" (what the app's own
placeholders suggest), "244123456", "233244123456", "+233 24 412 3456" - and
the users table already holds both "+233..." and "0..." forms. Exact string
matching therefore made a real user come back as "No user found".

Canonical form is E.164 ("+233" + 9 digits). New records are stored that
way; lookups match every equivalent spelling so existing rows stored in
local form keep working without a risky rewrite of a unique column.
"""

GH_DIAL_CODE = "233"


def normalize_gh_phone(raw: str) -> str:
    """Canonical "+233XXXXXXXXX" for anything recognisable as a Ghana mobile
    number; otherwise the stripped input unchanged (e.g. a foreign number),
    so validation elsewhere still sees what the user typed."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 12 and digits.startswith(GH_DIAL_CODE):
        return "+" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return f"+{GH_DIAL_CODE}{digits[1:]}"
    if len(digits) == 9:
        return f"+{GH_DIAL_CODE}{digits}"
    return raw.strip()


def phone_lookup_variants(raw: str) -> list[str]:
    """Every stored spelling that means the same number as `raw`."""
    canonical = normalize_gh_phone(raw)
    variants = {raw.strip(), canonical}
    if canonical.startswith("+" + GH_DIAL_CODE) and len(canonical) == 13:
        local = canonical[4:]
        variants.update({f"0{local}", f"{GH_DIAL_CODE}{local}", local})
    return [v for v in variants if v]
