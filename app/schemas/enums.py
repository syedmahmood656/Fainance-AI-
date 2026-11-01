"""Enums and the category list. Single source of truth: prompts, validation,
Telegram buttons and reports all read from here, so changing a category is a
one-place edit."""
from enum import Enum
from typing import Optional


class ExpenseCategory(str, Enum):
    PRINTING_STATIONARY = "Printing and stationary"
    CONVEYANCE = "conveyance"
    TRAVELLING_EXPENSES = "travelling expenses"
    MOBILE_RECHARGE = "mobile recharge"
    INTERNET = "internet"
    BOOKS_LIBRARY = "books and library"
    GUEST_HOSPITALITY = "guest hospitality"
    SUMMER_ISLAMIC_CAMP = "summer islamic camp"
    ISLAMIC_VOCATIONAL_CLASSES = "islamic vocational classes"
    INFAQ = "infaq"
    ZAKATH = "zakath"
    ZONAL_SHARE = "zonal share"
    MISCELLANEOUS = "Miscellaneous"


# Used in the categorization prompt so the model knows what each bucket means.
CATEGORY_DESCRIPTIONS = {
    ExpenseCategory.PRINTING_STATIONARY: "printing, stationery, paper, consumables, office supplies",
    ExpenseCategory.CONVEYANCE: "local taxi, cab, auto, fuel, tolls, conveyance",
    ExpenseCategory.TRAVELLING_EXPENSES: "flights, train/bus tickets, travel bookings, visa fees",
    ExpenseCategory.MOBILE_RECHARGE: "mobile recharge, data packs, phone credit",
    ExpenseCategory.INTERNET: "internet, broadband bills, wifi",
    ExpenseCategory.BOOKS_LIBRARY: "books, library subscriptions, educational journals",
    ExpenseCategory.GUEST_HOSPITALITY: "guest houses, hospitality, refreshments for guests",
    ExpenseCategory.SUMMER_ISLAMIC_CAMP: "summer islamic camp fees, camp materials",
    ExpenseCategory.ISLAMIC_VOCATIONAL_CLASSES: "vocational classes, tuition, course fees",
    ExpenseCategory.INFAQ: "voluntary charity, general infaq",
    ExpenseCategory.ZAKATH: "obligatory zakath payments",
    ExpenseCategory.ZONAL_SHARE: "zonal share contributions",
    ExpenseCategory.MISCELLANEOUS: "anything that does not clearly fit the categories above",
}

ALL_CATEGORIES = list(ExpenseCategory)

# Common alternative spellings a model (or a human) might produce -> your category.
# Keys are lower-case; "&" is treated as "and". Add your own words here.
_C = ExpenseCategory
_CATEGORY_ALIASES = {
    "printing": _C.PRINTING_STATIONARY, "stationary": _C.PRINTING_STATIONARY, "stationery": _C.PRINTING_STATIONARY,
    "printing and stationery": _C.PRINTING_STATIONARY, "printing and stationary": _C.PRINTING_STATIONARY,
    "office supplies": _C.PRINTING_STATIONARY, "xerox": _C.PRINTING_STATIONARY,
    "transport": _C.CONVEYANCE, "transportation": _C.CONVEYANCE, "taxi": _C.CONVEYANCE, "cab": _C.CONVEYANCE,
    "auto": _C.CONVEYANCE, "fuel": _C.CONVEYANCE,
    "travel": _C.TRAVELLING_EXPENSES, "travelling": _C.TRAVELLING_EXPENSES, "traveling": _C.TRAVELLING_EXPENSES,
    "travel expenses": _C.TRAVELLING_EXPENSES, "travelling expense": _C.TRAVELLING_EXPENSES,
    "recharge": _C.MOBILE_RECHARGE, "mobile": _C.MOBILE_RECHARGE, "phone recharge": _C.MOBILE_RECHARGE,
    "wifi": _C.INTERNET, "broadband": _C.INTERNET, "internet bill": _C.INTERNET,
    "books": _C.BOOKS_LIBRARY, "library": _C.BOOKS_LIBRARY, "book": _C.BOOKS_LIBRARY,
    "books and libraries": _C.BOOKS_LIBRARY,
    "hospitality": _C.GUEST_HOSPITALITY, "guest": _C.GUEST_HOSPITALITY, "guests": _C.GUEST_HOSPITALITY,
    "camp": _C.SUMMER_ISLAMIC_CAMP, "summer camp": _C.SUMMER_ISLAMIC_CAMP, "islamic camp": _C.SUMMER_ISLAMIC_CAMP,
    "vocational": _C.ISLAMIC_VOCATIONAL_CLASSES, "vocational classes": _C.ISLAMIC_VOCATIONAL_CLASSES,
    "vocational class": _C.ISLAMIC_VOCATIONAL_CLASSES,
    "infak": _C.INFAQ,
    "zakat": _C.ZAKATH, "zakaat": _C.ZAKATH, "zakah": _C.ZAKATH,
    "zonal": _C.ZONAL_SHARE, "zone share": _C.ZONAL_SHARE,
    "misc": _C.MISCELLANEOUS, "other": _C.MISCELLANEOUS, "others": _C.MISCELLANEOUS,
}
del _C


def normalize_category(value) -> Optional[ExpenseCategory]:
    """Map free text to an ExpenseCategory, or None when it can't be matched."""
    if value is None:
        return None
    if isinstance(value, ExpenseCategory):
        return value
    text = str(value).strip().lower().replace("&", "and")
    text = " ".join(text.split())
    if not text:
        return None
    for cat in ExpenseCategory:
        if text == cat.value.lower().replace("&", "and"):
            return cat
    return _CATEGORY_ALIASES.get(text)


class GoodsOrService(str, Enum):
    GOODS = "Goods"
    SERVICE = "Service"


def normalize_goods_or_service(value) -> Optional[GoodsOrService]:
    if value is None:
        return None
    if isinstance(value, GoodsOrService):
        return value
    text = str(value).strip().lower()
    if text in ("goods", "good", "product", "products", "item", "items", "g"):
        return GoodsOrService.GOODS
    if text in ("service", "services", "s"):
        return GoodsOrService.SERVICE
    return None


class ExpenseStatus(str, Enum):
    PENDING = "pending"          # extracted, waiting for the human
    CONFIRMED = "confirmed"      # human confirmed -> counted in reports
    DISCARDED = "discarded"      # human rejected the extraction
    EXPIRED = "expired"          # never confirmed within the TTL


class ReportIntent(str, Enum):
    SUMMARY = "summary"                      # total + count
    CATEGORY_BREAKDOWN = "category_breakdown"
    MONTHLY_TREND = "monthly_trend"
    YEARLY_SUMMARY = "yearly_summary"
    CHART = "chart"                          # user explicitly asked for a graph
    EXCEL_REPORT = "excel_report"
    EXPENSE_LIST = "expense_list"
    RECEIPT_LOOKUP = "receipt_lookup"
    UNKNOWN = "unknown"


class ChartKind(str, Enum):
    CATEGORY_BAR = "category_bar"
    CATEGORY_PIE = "category_pie"            # category distribution
    TOP_CATEGORIES = "top_categories"
    MONTHLY_BAR = "monthly_bar"
    YEARLY_TREND = "yearly_trend"
    VENDOR_BAR = "vendor_bar"
    TIMELINE = "timeline"                    # line graph: expense amount x time


class PeriodKind(str, Enum):
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    THIS_YEAR = "this_year"
    LAST_YEAR = "last_year"
    MONTH = "month"                          # needs month (+ optional year)
    YEAR = "year"                            # needs year
    LAST_N_MONTHS = "last_n_months"          # needs months
    CUSTOM = "custom"                        # needs start_date and/or end_date
    ALL = "all"


# Fields a human may correct, in the order shown in the Telegram menu.
CORRECTABLE_FIELDS = [
    ("vendor", "Vendor"),
    ("expense_date", "Date"),
    ("category", "Category"),
    ("item", "Item"),
    ("goods_or_service", "Goods/Service"),
    ("expense_amount", "Amount"),
]
CORRECTABLE_FIELD_KEYS = [k for k, _ in CORRECTABLE_FIELDS]
