from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import requests

# Fallback rates: 1 unit of foreign currency -> INR (used only if both live APIs fail)
FALLBACK_TO_INR: dict[str, float] = {
    "INR": 1.0,
    "USD": 85.0,
    "EUR": 92.0,
    "GBP": 108.0,
    "CAD": 61.0,
    "AUD": 55.0,
    "SGD": 63.0,
    "AED": 23.1,
    "CHF": 96.0,
    "JPY": 0.56,
    "NZD": 50.0,
    "SEK": 8.0,
    "NOK": 7.8,
    "PLN": 21.5,
}

SYMBOL_TO_ISO: dict[str, str] = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "c$": "CAD",
    "ca$": "CAD",
    "cad": "CAD",
    "a$": "AUD",
    "au$": "AUD",
    "aud": "AUD",
    "s$": "SGD",
    "sgd": "SGD",
    "chf": "CHF",
    "aed": "AED",
    "¥": "JPY",
    "jpy": "JPY",
    "pln": "PLN",
    "sek": "SEK",
    "nok": "NOK",
}


@dataclass(frozen=True)
class FXSnapshot:
    rates_to_inr: dict[str, float]  # e.g. {"USD": 84.92, "EUR": 91.40, "INR": 1.0}
    provider: str
    updated_utc: str


def fetch_fx_to_inr(timeout: int = 10) -> FXSnapshot:
    """
    Fetches live daily FX rates against INR.
    Primary: ExchangeRate-API Open Access (160+ currencies, no key).
    Fallback: Frankfurter (ECB daily rates, no key).
    Final Fallback: Built-in static snapshot.
    """
    # 1. Primary: open.er-api.com (returns 1 INR = X FOREIGN; invert for 1 FOREIGN = Y INR)
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/INR", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("result") == "success" and payload.get("rates"):
            rates_to_inr = {"INR": 1.0}
            for code, per_inr in payload["rates"].items():
                if per_inr and per_inr > 0:
                    rates_to_inr[code.upper()] = round(1.0 / float(per_inr), 6)
            updated = payload.get("time_last_update_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return FXSnapshot(
                rates_to_inr=rates_to_inr,
                provider="ExchangeRate-API (open.er-api.com)",
                updated_utc=updated,
            )
    except Exception:
        pass

    # 2. Fallback: Frankfurter (ECB)
    try:
        resp = requests.get("https://api.frankfurter.app/latest?from=INR", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("rates"):
            rates_to_inr = {"INR": 1.0}
            for code, per_inr in payload["rates"].items():
                if per_inr and per_inr > 0:
                    rates_to_inr[code.upper()] = round(1.0 / float(per_inr), 6)
            return FXSnapshot(
                rates_to_inr=rates_to_inr,
                provider="Frankfurter (ECB)",
                updated_utc=f"{payload.get('date', 'unknown')} (ECB daily)",
            )
    except Exception:
        pass

    # 3. Offline emergency fallback
    return FXSnapshot(
        rates_to_inr=dict(FALLBACK_TO_INR),
        provider="Static Fallback (Offline)",
        updated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def resolve_currency_code(raw: str | None, fallback: str | None = None) -> str | None:
    if raw:
        cleaned = str(raw).strip().lower()
        if cleaned in SYMBOL_TO_ISO:
            return SYMBOL_TO_ISO[cleaned]
        upper = cleaned.upper()
        if len(upper) == 3 and upper.isalpha():
            return upper
    return fallback