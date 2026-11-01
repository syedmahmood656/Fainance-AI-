"""Scoring and statistics for measuring extraction/categorization quality (pure Python).

Used by:
  scripts/export_eval_dataset.py   live usage: AI value vs what the human confirmed
  scripts/eval_receipts.py         labelled receipts: AI value vs your ground-truth CSV
"""
import math
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCORED_FIELDS = ["vendor", "expense_date", "expense_amount", "category", "invoice_number"]


# ---------------------------------------------------------------- statistics
def wilson_interval(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score interval for a proportion. Honest for small N (unlike mean +/- stderr)."""
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def proportion(successes: int, n: int) -> Dict[str, Any]:
    lo, hi = wilson_interval(successes, n)
    return {"k": successes, "n": n, "pct": round(100 * successes / n, 1) if n else None,
            "ci_low": round(100 * lo, 1), "ci_high": round(100 * hi, 1)}


def sample_size_note(n: int) -> str:
    if n < 20:
        return f"N={n} is too small to quote as a headline number (aim for 50+)."
    if n < 50:
        return f"N={n} is usable but wide-ranging; 50+ receipts would make the claim solid."
    return f"N={n} is a reasonable sample."


# ---------------------------------------------------------------- field comparison
def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def vendor_matches(truth: Any, predicted: Any, threshold: float = 0.8) -> bool:
    """Forgiving about case/punctuation/word order and one name containing the other
    ("The Lavish Restaurant" ~ "Lavish Restaurant"), strict about different businesses."""
    a, b = _norm(truth), _norm(predicted)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    if SequenceMatcher(None, a, b).ratio() >= threshold:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / max(len(ta), len(tb)) >= threshold


def amount_matches(truth: Any, predicted: Any) -> bool:
    try:
        return abs(float(truth) - float(predicted)) < 0.01
    except (TypeError, ValueError):
        return False


def exact_matches(truth: Any, predicted: Any) -> bool:
    return _norm(truth) == _norm(predicted) and _norm(truth) != ""


_COMPARATORS = {
    "vendor": vendor_matches,
    "expense_amount": amount_matches,
    "expense_date": lambda t, p: str(t) == str(p),
    "category": exact_matches,
    "invoice_number": exact_matches,
}


def score_receipt(truth: Dict[str, Any], predicted: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    """Per field: True/False, or None when the truth for that field was left blank (not scored)."""
    out: Dict[str, Optional[bool]] = {}
    for field in SCORED_FIELDS:
        wanted = truth.get(field)
        if wanted in (None, ""):
            out[field] = None
        else:
            out[field] = bool(_COMPARATORS[field](wanted, predicted.get(field)))
    return out


def summarize(scores: Iterable[Dict[str, Optional[bool]]]) -> Dict[str, Any]:
    """scores: one score_receipt() dict per receipt.

    'fully_correct' = every field that has a truth value was right
    (the labelled-set equivalent of 'accepted without any correction')."""
    scores = list(scores)
    per_field = {}
    for field in SCORED_FIELDS:
        vals = [s[field] for s in scores if s.get(field) is not None]
        if vals:
            per_field[field] = proportion(sum(vals), len(vals))
    full = [all(v for v in s.values() if v is not None) for s in scores if any(v is not None for v in s.values())]
    return {"n": len(scores), "fully_correct": proportion(sum(full), len(full)), "per_field": per_field}


# ---------------------------------------------------------------- reporting
def resume_line(n: int, accepted: Dict[str, Any], category: Dict[str, Any], label: str = "real receipts") -> str:
    """The sentence for a CV, with the sample size and the 95% interval so it survives an interview."""
    return (f"Evaluated on {n} {label}: {accepted['pct']}% accepted without any correction "
            f"(95% CI {accepted['ci_low']}–{accepted['ci_high']}%), category accuracy {category['pct']}% "
            f"(95% CI {category['ci_low']}–{category['ci_high']}%).")


def live_summary(records: List[Dict[str, Any]], discarded: int = 0) -> Dict[str, Any]:
    """records: confirmed receipts with 'corrected_by_user' and 'corrected_fields' (from the database).

    X = accepted untouched / confirmed.   Y = category kept / confirmed.
    'strict' also counts receipts the user discarded as failures (avoids survivorship bias)."""
    n = len(records)
    accepted = sum(1 for r in records if not r["corrected_by_user"])
    cat_ok = sum(1 for r in records if "category" not in (r["corrected_fields"] or []))
    out = {
        "n": n,
        "accepted": proportion(accepted, n),
        "category": proportion(cat_ok, n),
        "discarded": discarded,
        "accepted_strict": proportion(accepted, n + discarded),
        "per_field": {},
    }
    from app.schemas.enums import CORRECTABLE_FIELD_KEYS  # local import keeps this module dependency-free
    for field in CORRECTABLE_FIELD_KEYS:
        kept = sum(1 for r in records if field not in (r["corrected_fields"] or []))
        out["per_field"][field] = proportion(kept, n)
    return out
