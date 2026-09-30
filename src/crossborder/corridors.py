"""Bitnob payout corridors and their beneficiary requirements.

Bitnob publishes, per country, which currencies can be paid out and by which
method (mobile money, local bank, ACH/wire, SEPA, UK domestic, SWIFT...), and
for each method the exact fields it needs - type, required, regex, options,
nested fieldsets, and an individual/business "variant" for the sender:
  GET /api/payouts/supported-countries          (111 countries, 2026-09-29)
  GET /api/payouts/supported-countries/{code}   (fields per destination type)

The app builds its transfer form from this and the backend validates against
the same schema BEFORE quoting or taking payment, so the form can't drift from
what Bitnob accepts. Probed live: Nigeria offers ONLY bank payouts (not mobile
money - the sandbox still "succeeded" a mobile-money NG payout, which would
fail live), Kenya offers mobile_money/paybill/paytill/swift, the US needs
ach/wire, Europe bank (IBAN)/swift, the UK domestic_gbp/sepa_eur/swift.
"""

import re
import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from src.config import settings
from src.crossborder import bitnob

_TTL_SECONDS = 3600
_cache: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, path: str) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
        return hit[1]
    data = (await bitnob.request("GET", path)).get("data")
    _cache[key] = (time.monotonic(), data)
    return data


async def list_countries() -> list[dict]:
    """[{code, name, flag, dial_code, corridors: [{currency, destination_types: [...]}]}]"""
    data = await _cached("countries", "/api/payouts/supported-countries")
    items = data if isinstance(data, list) else (data or {}).get("countries", [])
    return sorted((c for c in items if isinstance(c, dict)), key=lambda c: c.get("name", ""))


async def get_requirements(country: str) -> dict:
    """{code, name, destination_types: {key: {label, fields, banks, limits, ...}}}"""
    return await _cached(f"req:{country}", f"/api/payouts/supported-countries/{country}")


async def destination_types_for(country: str, currency: str) -> list[str]:
    for c in await list_countries():
        if c.get("code") == country:
            for corridor in c.get("corridors", []):
                if corridor.get("currency") == currency:
                    return list(corridor.get("destination_types", []))
    return []


def _options(field: dict, dest: dict) -> list[str]:
    opts = [str(o.get("value")) for o in field.get("options") or [] if o.get("value") is not None]
    if not opts and field.get("key") == "bank_code":
        opts = [str(b.get("code")) for b in dest.get("banks") or [] if b.get("code")]
    return opts


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _validate_fields(fields: list[dict], values: Any, dest: dict, path: str, errors: list[str]) -> dict:
    """Checks `values` against Bitnob's field list and returns the cleaned
    object to send. Select values are matched case/punctuation-insensitively
    and replaced with Bitnob's exact option ("M-Pesa" -> "mpesa")."""
    values = values if isinstance(values, dict) else {}
    cleaned: dict[str, Any] = {}
    for field in fields:
        key = field.get("key")
        if not key or field.get("hidden"):
            continue
        label = f"{path}{field.get('label') or key}"
        component = field.get("component")
        raw = values.get(key)

        if component == "fieldset":
            cleaned[key] = _validate_fields(field.get("fields") or [], raw, dest, f"{label} - ", errors)
            continue
        if component == "variant_fieldset":
            variant_key = field.get("variant_key") or "type"
            raw = raw if isinstance(raw, dict) else {}
            variant = raw.get(variant_key)
            variants = field.get("variants") or {}
            if variant not in variants:
                errors.append(f"{label}: choose {' or '.join(variants) or 'a type'}")
                continue
            spec = variants[variant]
            sub = _validate_fields(spec if isinstance(spec, list) else spec.get("fields") or [], raw, dest, f"{label} - ", errors)
            cleaned[key] = {variant_key: variant, **sub}
            continue

        value = "" if raw is None else str(raw).strip()
        if not value:
            if field.get("required"):
                errors.append(f"{label} is required")
            continue
        if component == "country_select":
            if not re.fullmatch(r"[A-Za-z]{2}", value):
                errors.append(f"{label}: choose a country")
                continue
            value = value.upper()
        options = _options(field, dest)
        if options:
            match = next((o for o in options if _canonical(o) == _canonical(value)), None)
            if match is None:
                errors.append(f"{label}: '{value}' isn't one of the allowed options")
                continue
            value = match
        if field.get("pattern") and not re.fullmatch(field["pattern"], value):
            example = f" (e.g. {field['placeholder'].removeprefix('e.g. ')})" if field.get("placeholder") else ""
            errors.append(f"{label} doesn't look right{example}")
            continue
        if field.get("min_length") and len(value) < int(field["min_length"]):
            errors.append(f"{label} must be at least {field['min_length']} characters")
            continue
        if field.get("max_length") and len(value) > int(field["max_length"]):
            errors.append(f"{label} must be at most {field['max_length']} characters")
            continue
        cleaned[key] = value
    return cleaned


async def build_beneficiary(country: str, currency: str, destination_type: str, values: dict) -> tuple[dict, dict]:
    """Validate a beneficiary against Bitnob's live requirements. Returns
    (beneficiary payload for Bitnob, destination-type spec incl. limits);
    raises ValueError with every problem found."""
    allowed = await destination_types_for(country, currency)
    if destination_type not in allowed:
        raise ValueError(
            f"{currency} payouts to {country} can't use '{destination_type}'"
            + (f" - choose {', '.join(allowed)}" if allowed else " - this corridor isn't supported")
        )
    requirements = await get_requirements(country)
    dest = (requirements.get("destination_types") or {}).get(destination_type)
    if not dest:
        raise ValueError(f"'{destination_type}' isn't available for {country}")
    errors: list[str] = []
    cleaned = _validate_fields(dest.get("fields") or [], values, dest, "", errors)
    if errors:
        raise ValueError("; ".join(errors))
    return {**cleaned, "destination_type": destination_type, "country": country}, dest


async def bank_name(details: dict) -> str | None:
    """Display name of the beneficiary's bank: the typed bank_name if the form
    had one (US ACH/wire), else the bank_code looked up in Bitnob's bank list
    (Nigeria). None if unknown or Bitnob's list can't be loaded right now."""
    typed = details.get("bank_name") or (details.get("beneficiary") or {}).get("bank_name")
    if typed:
        return typed
    code, country, dest_type = details.get("bank_code"), details.get("country"), details.get("destination_type")
    if not (code and country and dest_type):
        return None
    try:
        requirements = await get_requirements(country)
    except bitnob.BitnobError:
        return None
    dest = (requirements.get("destination_types") or {}).get(dest_type) or {}
    return next((b.get("name") for b in dest.get("banks") or [] if str(b.get("code")) == str(code)), None)


_RATE_TTL_SECONDS = 600
_RATE_PROBE_USDC = "100"  # above every corridor minimum, below every maximum (checked 2026-09-29)
_rates: dict[tuple[str, str], tuple[float, Decimal]] = {}


async def _usdc_rate(country: str, currency: str) -> Decimal:
    """USDC -> `currency` rate from a throwaway Bitnob quote. A quote moves no
    money (only finalize does) and simply expires; cached for 10 minutes."""
    hit = _rates.get((country, currency))
    if hit and time.monotonic() - hit[0] < _RATE_TTL_SECONDS:
        return hit[1]
    quote = await bitnob.create_quote(
        from_asset="USDC", to_currency=currency, country=country,
        amount=_RATE_PROBE_USDC, reference=f"rate-probe-{uuid.uuid4().hex[:12]}",
    )
    payout = quote.get("data", {}).get("payout", {})
    rate = Decimal(str(payout.get("exchange_rate", {}).get("effective_rate") or "0"))
    if rate <= 0:
        raise ValueError("Bitnob quote had no rate")
    _rates[(country, currency)] = (time.monotonic(), rate)
    return rate


async def amount_limits(country: str, currency: str, destination_type: str) -> dict:
    """Bitnob's min/max for this payout, in the destination currency and as an
    approximate GHS amount to type in (min rounded up, max down). The GHS
    figures are None if no rate could be fetched - the destination limits
    still show, and initiate re-checks the real quote anyway."""
    requirements = await get_requirements(country)
    dest = (requirements.get("destination_types") or {}).get(destination_type) or {}
    limits = dest.get("limits") or {}
    if limits.get("currency") not in (None, currency):
        limits = {}
    try:
        low = Decimal(str(limits["min_amount"])) if limits.get("min_amount") is not None else None
        high = Decimal(str(limits["max_amount"])) if limits.get("max_amount") is not None else None
    except (InvalidOperation, ValueError):
        low = high = None

    min_ghs = max_ghs = None
    if low is not None or high is not None:
        try:
            ghs_per_unit = Decimal(str(settings.DEMO_GHS_USD_RATE)) / await _usdc_rate(country, currency)
            if low is not None:
                min_ghs = (low * ghs_per_unit).quantize(Decimal("1"), rounding=ROUND_CEILING)
            if high is not None:
                max_ghs = (high * ghs_per_unit).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        except (bitnob.BitnobError, ValueError, InvalidOperation):
            pass
    return {"currency": currency, "min_amount": low, "max_amount": high, "min_ghs": min_ghs, "max_ghs": max_ghs}


def check_limits(dest: dict, destination_amount: Decimal | None, currency: str) -> None:
    """Bitnob's per-corridor min/max, in the destination currency."""
    limits = dest.get("limits") or {}
    if destination_amount is None or limits.get("currency") not in (None, currency):
        return
    try:
        low = Decimal(str(limits["min_amount"])) if limits.get("min_amount") is not None else None
        high = Decimal(str(limits["max_amount"])) if limits.get("max_amount") is not None else None
    except (InvalidOperation, ValueError):
        return
    if low is not None and destination_amount < low:
        raise ValueError(f"That's {destination_amount} {currency}; the minimum for this payout is {low} {currency}")
    if high is not None and destination_amount > high:
        raise ValueError(f"That's {destination_amount} {currency}; the maximum for this payout is {high} {currency}")
