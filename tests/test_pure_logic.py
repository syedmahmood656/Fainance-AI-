"""Tests for the pure-Python parts (no DB, no LLM, no network)."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app.agent.reporting_flow import build_reply, plan_tool_calls
from app.ai.llm_utils import (BadModelOutput, classify_error, cooldown_seconds, extract_json,
                              prepare_image_for_llm, select_free_models)
from app.ai.report_rules import rule_based_request
from app.reporting.periods import resolve_period
from app.schemas.enums import (ChartKind, ExpenseCategory, PeriodKind, ReportIntent, normalize_category,
                               normalize_goods_or_service)
from app.services.feedback import compute_corrections
from app.services.validation import card_warnings, sanitize_extraction
from app.utils.formatting import format_compact, format_expense_card, format_money
from app.utils.parsing import (normalize_currency, parse_amount, parse_correction, parse_date_flexible)

TODAY = date(2026, 10, 12)


# ---------------- parsing ----------------
@pytest.mark.parametrize("raw,expected", [
    ("₹2,450.00", 2450.0), ("Rs 2450", 2450.0), ("1.234,56", 1234.56), ("1,23,456", 123456.0),
    ("2450/-", 2450.0), ("abc", None), (None, None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-08", date(2026, 10, 8)), ("08/10/2026", date(2026, 10, 8)), ("8 Oct 2026", date(2026, 10, 8)),
    ("Oct 8, 2026", date(2026, 10, 8)), ("31/02/2026", None), ("garbage", None),
])
def test_parse_date_is_day_first(raw, expected):
    assert parse_date_flexible(raw) == expected


def test_currency_never_invented():
    assert normalize_currency("₹") == "INR"
    assert normalize_currency("abc") is None
    assert normalize_currency(None) is None


def test_parse_correction_validates():
    assert parse_correction("expense_amount", "₹12,050") == 12050.0
    assert parse_correction("category", "printing & stationary") == ExpenseCategory.PRINTING_STATIONARY.value
    with pytest.raises(ValueError):
        parse_correction("expense_amount", "free")
    with pytest.raises(ValueError):
        parse_correction("expense_date", "soon")


def test_category_normalization():
    assert normalize_category("Travelling Expenses") == ExpenseCategory.TRAVELLING_EXPENSES
    assert normalize_category("taxi") == ExpenseCategory.CONVEYANCE
    assert normalize_category("Zakat") == ExpenseCategory.ZAKATH
    assert normalize_category("Equipment") is None       # not one of your categories -> never guessed
    assert normalize_goods_or_service("services").value == "Service"


# ---------------- LLM output handling ----------------
def test_extract_json_handles_fences_think_and_chatter():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>{"x":1}</think>{"category": "Travel"}') == {"category": "Travel"}
    assert extract_json('Sure! {"a": {"b": "}"}} bye') == {"a": {"b": "}"}}
    with pytest.raises(BadModelOutput):
        extract_json("no json here")


def test_error_classification_covers_openrouter_token_errors():
    class E(Exception):
        def __init__(self, msg, code=None):
            super().__init__(msg)
            self.status_code = code

    assert classify_error(E("requires more credits, or fewer max_tokens. can only afford 1200", 402)) == "credits"
    assert classify_error(E("maximum context length is 8192 tokens", 400)) == "context"
    assert classify_error(E("rate limit", 429)) == "rate_limit"
    assert classify_error(E("No endpoints found for x:free", 404)) == "not_found"
    # one model being forbidden must NOT be treated as a bad API key (that would disable the whole provider)
    assert classify_error(E("403 thinkingmachines/x:free is only available on agentic harnesses", 403)) == "forbidden"
    assert classify_error(E("Error code: 401 - User not found.", 401)) == "auth"
    assert classify_error(BadModelOutput("empty response")) == "empty"
    assert classify_error(BadModelOutput("no JSON object found in response")) == "bad_output"
    assert cooldown_seconds("empty", 300) == 150 and cooldown_seconds("forbidden", 300) > 0
    assert cooldown_seconds("bad_output", 300) == 0
    assert cooldown_seconds("rate_limit", 300) == 300


def test_select_free_models_filters_paid_and_non_vision():
    payload = {"data": [
        {"id": "paid/x", "pricing": {"prompt": "0.1", "completion": "0.1"}, "context_length": 9,
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "a/vision:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 1000,
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "b/text:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 5000,
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    ]}
    assert select_free_models(payload, vision=True) == ["a/vision:free"]
    assert select_free_models(payload, vision=False) == ["b/text:free", "a/vision:free"]


def test_large_image_is_downscaled():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(buf, "PNG")
    out = prepare_image_for_llm(buf.getvalue(), max_side=1280)
    assert max(Image.open(io.BytesIO(out)).size) == 1280
    with pytest.raises(ValueError):
        prepare_image_for_llm(b"not an image")


# ---------------- periods ----------------
def test_periods():
    assert resolve_period("month", TODAY, month=9)[:2] == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_period("month", TODAY, month=12)[0] == date(2025, 12, 1)   # not in the future
    assert resolve_period("last_month", date(2026, 1, 15))[:2] == (date(2025, 12, 1), date(2025, 12, 31))
    start, end, _ = resolve_period("last_n_months", TODAY, months=6)
    assert (start, end) == (date(2026, 5, 1), date(2026, 10, 31))
    assert resolve_period("all", TODAY)[:2] == (None, None)


# ---------------- rule-based report parsing (fallback when LLMs are down) ----------------
@pytest.mark.parametrize("text,intent", [
    ("Show me our expenses by category for September.", "category_breakdown"),
    ("Show me a bar graph of this month's expenses by category.", "chart"),
    ("Show me the last 6 months of expenses.", "monthly_trend"),
    ("Generate this month's Excel report.", "excel_report"),
    ("Download the yearly expense report.", "excel_report"),
    ("Show me the receipt for the ₹2,450 restaurant expense.", "receipt_lookup"),
    ("Show me September receipts.", "receipt_lookup"),
    ("How much did we spend this year?", "summary"),
    ("hello there", "unknown"),
])
def test_rule_parser_intents(text, intent):
    assert rule_based_request(text, TODAY)["intent"] == intent


def test_rule_parser_details():
    r = rule_based_request("Give me an Excel report for Travel expenses in September", TODAY)
    assert r["category"] == ExpenseCategory.TRAVELLING_EXPENSES.value and r["period"] == {"kind": "month", "month": 9}
    assert rule_based_request("show the receipt for the ₹6,500 hotel expense", TODAY)["amount"] == 6500.0
    assert rule_based_request("bar graph of this month's expenses by category", TODAY)["chart_type"] == "category_bar"


# ---------------- tool planning is deterministic ----------------
def _req(intent, kind="this_month", chart=None, **kw):
    period = NS(kind=PeriodKind(kind), year=kw.get("year"), month=kw.get("month"), months=kw.get("months"),
                start_date=None, end_date=None)
    cat = NS(value=kw["category"]) if kw.get("category") else None
    return NS(intent=ReportIntent(intent), chart_type=ChartKind(chart) if chart else None, period=period,
              category=cat, top_n=kw.get("top_n"))


def test_category_breakdown_calls_data_tool_then_chart_tool():
    calls = plan_tool_calls(_req("category_breakdown", "month", month=9), None, TODAY)
    assert [c[0] for c in calls] == ["get_category_breakdown", "generate_category_chart"]
    assert calls[0][1]["start_date"] == "2026-09-01" and calls[0][1]["end_date"] == "2026-09-30"


def test_monthly_trend_never_uses_a_single_month():
    calls = plan_tool_calls(_req("monthly_trend"), None, TODAY)
    assert calls[0][1]["start_date"] == "2026-05-01"


def test_excel_and_unknown_intents():
    calls = plan_tool_calls(_req("excel_report", "month", month=9, category="Travel"), 5, TODAY)
    assert calls == [("generate_expense_report", {
        "user_id": 5, "start_date": "2026-09-01", "end_date": "2026-09-30", "category": "Travel",
        "period_label": "September 2026"})]
    assert plan_tool_calls(_req("unknown"), None, TODAY) == []


def test_reply_uses_only_tool_numbers():
    rows = [{"category": "Travel", "total": 42000.0, "count": 5, "percent": 60.0},
            {"category": "Food & Meals", "total": 28000.0, "count": 9, "percent": 40.0}]
    base = {"period_label": "September 2026", "currency": "INR", "other_currency_count": 0, "empty": False}
    calls = [("get_category_breakdown", {}), ("generate_category_chart", {})]
    reply = build_reply(calls, [{**base, "total": 70000.0, "count": 14, "rows": rows},
                                {**base, "total": 70000.0, "rows": rows, "chart_path": "/x.png"}])
    assert "Travel — ₹42,000 (60%)" in reply["text"] and "₹70,000" in reply["text"]
    assert reply["chart_paths"] == ["/x.png"]


def test_reply_for_empty_period():
    reply = build_reply([("get_expense_summary", {})],
                        [{"period_label": "May 2026", "currency": "INR", "empty": True, "category": None}])
    assert reply["text"] == "No confirmed expenses found for May 2026."


# ---------------- formatting ----------------
def test_money_formatting():
    assert format_money(2450, "INR") == "₹2,450"
    assert format_money(12345678, "INR") == "₹1,23,45,678"
    assert format_money(1234.5, "USD") == "$1,234.50"
    assert format_compact(150000, "INR") == "1.5L"


def test_confirmation_card_matches_the_spec_example():
    e = NS(vendor="ABC Restaurant", expense_date=date(2026, 10, 8), category="Food & Meals", item="Team Dinner",
           goods_or_service="Service", expense_amount=2450, currency="INR", invoice_number=None)
    assert format_expense_card(e) == (
        "🧾 Receipt processed\n\nVendor: ABC Restaurant\nDate: 08 Oct 2026\nCategory: Food & Meals\n"
        "Item: Team Dinner\nType: Service\nExpense: ₹2,450")


# ---------------- validation & feedback records ----------------
def test_sanitize_never_invents_but_defaults_currency_transparently():
    cleaned, assumed = sanitize_extraction(
        {"expense_amount": -5, "expense_date": date(2031, 1, 1), "currency": None}, TODAY)
    assert cleaned["expense_amount"] is None and cleaned["expense_date"] is None
    assert assumed == ["currency"]


def test_warnings_disappear_once_fixed():
    e = NS(expense_amount=None, expense_date=date(2026, 10, 8), category="Travel", currency="INR",
           subtotal=None, tax=None)
    assert card_warnings(e, TODAY, {})
    e.expense_amount = 100
    assert card_warnings(e, TODAY, {}) == []


def test_corrections_record_ai_vs_human_values():
    e = NS(vendor="XYZ", vendor_ai="XYZ", category="Equipment", category_ai="Office Supplies",
           expense_amount=Decimal("12050.00"), expense_amount_ai=Decimal("12500.00"),
           expense_date=date(2026, 10, 8), expense_date_ai=date(2026, 10, 8),
           item="Bag", item_ai="Bag", goods_or_service=None, goods_or_service_ai=None)
    assert compute_corrections(e) == [("category", "Office Supplies", "Equipment"),
                                      ("expense_amount", "12500.00", "12050.00")]


# ---------------- feature: category bar chart & expense x timeline ----------------
def test_timeline_granularity_is_chosen_from_the_range():
    from app.reporting.timeline import choose_granularity
    assert choose_granularity(date(2026, 10, 1), date(2026, 10, 31)) == "day"
    assert choose_granularity(date(2026, 5, 1), date(2026, 10, 31)) == "week"
    assert choose_granularity(date(2026, 1, 1), date(2026, 12, 31)) == "month"
    assert choose_granularity(date(2026, 1, 1), date(2026, 12, 31), "day") == "day"


def test_timeline_fills_gaps_accumulates_and_stops_at_today():
    from app.reporting.timeline import bucket_series
    rows = [{"date": date(2026, 10, 2), "total": 100.0, "count": 1},
            {"date": date(2026, 10, 5), "total": 250.5, "count": 2}]
    pts = bucket_series(rows, date(2026, 10, 1), date(2026, 10, 31), "day", today=date(2026, 10, 6))
    assert [p["label"] for p in pts] == ["01 Oct", "02 Oct", "03 Oct", "04 Oct", "05 Oct", "06 Oct"]
    assert [p["total"] for p in pts] == [0.0, 100.0, 0.0, 0.0, 250.5, 0.0]
    assert pts[-1]["cumulative"] == 350.5
    weeks = bucket_series(rows, date(2026, 9, 28), date(2026, 10, 11), "week")
    assert [p["label"] for p in weeks] == ["28 Sep", "05 Oct"]   # weeks start on Monday
    assert [p["total"] for p in weeks] == [100.0, 250.5]         # Fri 2 Oct -> week of 28 Sep; Mon 5 Oct -> next week
    assert bucket_series([], None, None, "day") == []


@pytest.mark.parametrize("text,gran,cumulative", [
    ("Show me a graph of expenses over time", None, False),
    ("daily expenses chart this month", "day", False),
    ("weekly expense graph for Travel in September", "week", False),
    ("cumulative expense timeline last 3 months", None, True),
    ("expense vs time for October", None, False),
])
def test_rule_parser_recognises_timeline_requests(text, gran, cumulative):
    r = rule_based_request(text, TODAY)
    assert r["intent"] == "chart" and r["chart_type"] == "timeline"
    assert r.get("granularity") == gran and r["cumulative"] is cumulative


def test_timeline_does_not_hijack_other_requests():
    assert rule_based_request("Show me the last 6 months of expenses.", TODAY)["intent"] == "monthly_trend"
    assert rule_based_request("Show me a bar graph of this month's expenses by category.", TODAY)["chart_type"] == "category_bar"


def test_planner_picks_timeline_tool_with_options():
    req = _req("chart", "month", chart="timeline", month=9, category="Travel")
    req.granularity, req.cumulative = "week", True
    (name, args), = plan_tool_calls(req, None, TODAY)
    assert name == "generate_timeline_chart"
    assert (args["granularity"], args["cumulative"], args["category"]) == ("week", True, "Travel")
    assert (args["start_date"], args["end_date"]) == ("2026-09-01", "2026-09-30")


def test_planner_category_bar_chart_request():
    (name, args), = plan_tool_calls(_req("chart", "this_month", chart="category_bar"), None, TODAY)
    assert (name, args["style"]) == ("generate_category_chart", "bar")


def test_timeline_reply_summarises_without_listing_every_day():
    pts = [{"bucket": "2026-10-01", "label": "01 Oct", "total": 100.0, "count": 1, "cumulative": 100.0},
           {"bucket": "2026-10-02", "label": "02 Oct", "total": 300.0, "count": 2, "cumulative": 400.0}]
    res = {"period_label": "October 2026", "currency": "INR", "other_currency_count": 0, "empty": False,
           "total": 400.0, "granularity": "day", "cumulative": False, "rows": pts, "chart_path": "/t.png"}
    reply = build_reply([("generate_timeline_chart", {})], [res])
    assert "Peak day: 02 Oct — ₹300" in reply["text"] and "Average per day: ₹200" in reply["text"]
    assert reply["chart_paths"] == ["/t.png"]


def test_charts_render_real_png_files():
    import os, tempfile
    from app.reporting import charts
    folder = tempfile.mkdtemp()
    cat = charts.horizontal_bars(["Travel", "Food & Meals"], [42000, 31500], "INR", "Expenses by category",
                                 "September 2026", os.path.join(folder, "cat.png"))
    line = charts.line_timeline(["01 Oct", "02 Oct", "03 Oct"], [0, 500, 200], "INR", "Expenses over time",
                                "October 2026", os.path.join(folder, "line.png"))
    for path in (cat, line):
        assert open(path, "rb").read(8) == b"\x89PNG\r\n\x1a\n" and os.path.getsize(path) > 3000


# ---------------- categories: one list drives everything ----------------
def test_category_list_is_consistent():
    from app.schemas.enums import ALL_CATEGORIES, CATEGORY_DESCRIPTIONS, _CATEGORY_ALIASES
    from app.ai.report_rules import _CATEGORY_WORDS
    values = [c.value for c in ALL_CATEGORIES]
    assert len(values) == len(set(v.lower() for v in values)), "duplicate category names"
    assert set(CATEGORY_DESCRIPTIONS) == set(ALL_CATEGORIES), "every category needs a description for the AI prompt"
    assert all(c in ALL_CATEGORIES for c in _CATEGORY_ALIASES.values())
    assert all(c in ALL_CATEGORIES for c in _CATEGORY_WORDS.values())
    for c in ALL_CATEGORIES:                       # every exact name resolves to itself, any casing
        assert normalize_category(c.value.upper()) == c and normalize_category(c.value) == c


def test_rule_parser_understands_your_categories():
    assert rule_based_request("Show me Zakat expenses this year", TODAY)["category"] == "zakath"
    assert rule_based_request("show infaq receipts", TODAY)["intent"] == "receipt_lookup"
    assert rule_based_request("guest hospitality by month", TODAY)["intent"] == "monthly_trend"


# ---------------- /add wizard (state machine against an in-memory store) ----------------
class FakeStore:
    def __init__(self):
        self.drafts, self.saved = {}, []

    def begin_manual(self, u): self.drafts[u] = ["amount", None, None]
    def get_manual(self, u): return tuple(self.drafts[u]) if u in self.drafts else None
    def set_manual_amount(self, u, a): self.drafts[u][0:2] = ["category", a]
    def clear_manual(self, u): self.drafts.pop(u, None)

    def set_manual_category(self, u, c):
        self.drafts[u][0], self.drafts[u][2] = "description", c

    def finish_manual(self, u, description):
        _step, amount, category = self.drafts.pop(u)
        e = NS(expense_amount=amount, currency="INR", category=category, expense_date=TODAY, description=description)
        self.saved.append(e)
        return e


def test_add_wizard_happy_path_with_buttons():
    from app.agent.manual_flow import handle_manual
    from app.schemas.enums import ALL_CATEGORIES
    store = FakeStore()
    assert "amount" in handle_manual(1, {"kind": "start"}, store)["response_text"]
    ask = handle_manual(1, {"kind": "text", "text": "₹1,250.50"}, store)
    assert "₹1,250.50" in ask["response_text"] and sum(len(r) for r in ask["buttons"]) == len(ALL_CATEGORIES)
    assert ask["buttons"][0][0][1] == "ma:0"
    nxt = handle_manual(1, {"kind": "category", "value": "2"}, store)
    assert ALL_CATEGORIES[2].value in nxt["response_text"] and nxt["edit_message"] is True
    done = handle_manual(1, {"kind": "text", "text": "Auto to the station"}, store)
    assert "added successfully" in done["response_text"] and "₹1,250.50" in done["response_text"]
    assert store.saved[0].category == ALL_CATEGORIES[2].value and store.saved[0].description == "Auto to the station"
    assert store.get_manual(1) is None


def test_add_wizard_rejects_bad_input_and_keeps_state():
    from app.agent.manual_flow import handle_manual
    store = FakeStore()
    handle_manual(1, {"kind": "start"}, store)
    for bad in ("abc", "-5", "0", "99999999999"):
        assert "positive number" in handle_manual(1, {"kind": "text", "text": bad}, store)["response_text"]
    assert store.get_manual(1)[0] == "amount"
    handle_manual(1, {"kind": "text", "text": "100"}, store)
    assert "don't know that category" in handle_manual(1, {"kind": "text", "text": "banana"}, store)["response_text"]
    assert handle_manual(1, {"kind": "category", "value": "99"}, store)["toast"] == "Invalid choice."
    handle_manual(1, {"kind": "text", "text": "zakat"}, store)               # typed category works too
    assert store.get_manual(1)[0] == "description"
    done = handle_manual(1, {"kind": "text", "text": "skip"}, store)
    assert store.saved[0].category == "zakath" and store.saved[0].description is None and "added" in done["response_text"]


def test_add_wizard_cancel_stale_and_wrong_step_buttons():
    from app.agent.manual_flow import handle_manual
    store = FakeStore()
    assert "Send /add" in handle_manual(1, {"kind": "text", "text": "hi"}, store)["response_text"]
    assert handle_manual(1, {"kind": "category", "value": "1"}, store)["toast"].startswith("That entry expired")
    handle_manual(1, {"kind": "start"}, store)
    assert handle_manual(1, {"kind": "category", "value": "1"}, store)["toast"] == "That button is no longer active."
    assert handle_manual(1, {"kind": "text", "text": "cancel"}, store)["response_text"] == "Okay, cancelled."
    assert store.get_manual(1) is None and not store.saved


# ---------------- duplicate receipts ----------------
def test_duplicate_upload_warning_on_the_card():
    e = NS(expense_amount=454, expense_date=date(2026, 10, 4), category="x", currency="INR", subtotal=None, tax=None)
    dup = {"id": 7, "status": "confirmed", "date": "2026-10-04", "amount": 454.0, "currency": "INR"}
    warning = card_warnings(e, TODAY, {"duplicate_of": dup})[0]
    assert "already uploaded" in warning and "#7" in warning and "04 Oct 2026" in warning and "₹454" in warning
    assert card_warnings(e, TODAY, {}) == []


# ---------------- evaluation maths ----------------
def test_wilson_interval_known_values_and_small_samples():
    from app.services.evaluation import proportion, sample_size_note, wilson_interval
    p = proportion(44, 62)
    assert (p["pct"], p["ci_low"], p["ci_high"]) == (71.0, 58.7, 80.8)
    assert wilson_interval(0, 0) == (0.0, 0.0)
    assert proportion(0, 5)["ci_high"] > 40                    # 0 of 5 does NOT prove 0%
    assert "too small" in sample_size_note(10) and "reasonable" in sample_size_note(80)


def test_vendor_matching_is_forgiving_but_not_sloppy():
    from app.services.evaluation import vendor_matches
    assert vendor_matches("The Lavish Restaurant", "LAVISH RESTAURANT")
    assert vendor_matches("Sree Chaitanya Institute of Technological Sciences", "Sree Chaitanya Institute")
    assert not vendor_matches("ABC Cafe", "XYZ Hotel") and not vendor_matches("Amazon", None)


def test_receipt_scoring_and_summary():
    from app.services.evaluation import score_receipt, summarize
    truth = {"vendor": "Lavish Restaurant", "expense_date": "2026-10-04", "expense_amount": 454, "category": "infaq",
             "invoice_number": ""}
    good = {"vendor": "The Lavish Restaurant", "expense_date": "2026-10-04", "expense_amount": 454.0, "category": "infaq"}
    bad = {**good, "category": "zakath", "expense_amount": 434.0}
    assert score_receipt(truth, good)["invoice_number"] is None          # blank truth = not scored
    assert score_receipt(truth, bad)["category"] is False and score_receipt(truth, bad)["expense_amount"] is False
    summary = summarize([score_receipt(truth, good), score_receipt(truth, bad)])
    assert summary["n"] == 2 and summary["fully_correct"]["k"] == 1
    assert summary["per_field"]["vendor"]["pct"] == 100.0 and summary["per_field"]["category"]["pct"] == 50.0


def test_live_summary_and_resume_sentence():
    from app.services.evaluation import live_summary, resume_line
    records = ([{"corrected_by_user": False, "corrected_fields": []}] * 7
               + [{"corrected_by_user": True, "corrected_fields": ["expense_amount"]}] * 2
               + [{"corrected_by_user": True, "corrected_fields": ["category"]}])
    s = live_summary(records, discarded=2)
    assert (s["n"], s["accepted"]["k"], s["category"]["k"]) == (10, 7, 9)
    assert s["accepted_strict"]["n"] == 12 and s["accepted_strict"]["k"] == 7     # discards count as failures
    line = resume_line(s["n"], s["accepted"], s["category"])
    assert "10 real receipts" in line and "70.0%" in line and "90.0%" in line and "95% CI" in line


def test_clock_is_naive_utc_without_deprecation():
    import warnings
    from app.utils.clock import utcnow
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert utcnow().tzinfo is None

def test_no_period_named_means_all_time_not_this_month():
    for text in ("Expense of every category", "show expenses by category", "how much did we spend", "internet expenses"):
        assert rule_based_request(text, TODAY)["period"] == {"kind": "all"}, text
    assert rule_based_request("expenses by category this month", TODAY)["period"] == {"kind": "this_month"}
    assert rule_based_request("expenses of every category in September", TODAY)["period"]["kind"] == "month"
    assert rule_based_request("Show me expenses over time", TODAY)["period"]["kind"] == "last_n_months"