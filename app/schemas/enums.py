"""Enums and the category list. Single source of truth: prompts, validation,
Telegram buttons and reports all read from here, so changing a category is a
one-place edit."""
from enum import Enum
from typing import Optional


class ExpenseCategory(str, Enum):
    TRAVEL = "Travel"
    FOOD_MEALS = "Food & Meals"
    ACCOMMODATION = "Accommodation"
    TRANSPORTATION = "Transportation"
    OFFICE_SUPPLIES = "Office Supplies"
    UTILITIES = "Utilities"
    SOFTWARE_SUBSCRIPTIONS = "Software & Subscriptions"
    EQUIPMENT = "Equipment"
    MARKETING = "Marketing"
    COMMUNICATION = "Communication"
    PROFESSIONAL_SERVICES = "Professional Services"
    MISCELLANEOUS = "Miscellaneous"


# Used in the categorization prompt so the model knows what each bucket means.
CATEGORY_DESCRIPTIONS = {
    ExpenseCategory.TRAVEL: "flights, train/bus tickets for trips, travel bookings, visa fees",
    ExpenseCategory.FOOD_MEALS: "restaurants, team meals, catering, snacks, refreshments",
    ExpenseCategory.ACCOMMODATION: "hotels, lodging, guest houses, stays",
    ExpenseCategory.TRANSPORTATION: "local taxi/cab/auto, fuel, parking, tolls, vehicle hire",
    ExpenseCategory.OFFICE_SUPPLIES: "stationery, printing, paper, consumables, small office goods",
    ExpenseCategory.UTILITIES: "electricity, water, gas, internet/broadband bills",
    ExpenseCategory.SOFTWARE_SUBSCRIPTIONS: "software licences, SaaS, cloud, app subscriptions",
    ExpenseCategory.EQUIPMENT: "laptops, phones, machinery, furniture, durable hardware",
    ExpenseCategory.MARKETING: "ads, banners, promotion, events, branding material",
    ExpenseCategory.COMMUNICATION: "mobile recharge, data packs, postage, courier",
    ExpenseCategory.PROFESSIONAL_SERVICES: "legal, accounting, consulting, freelancers, audits",
    ExpenseCategory.MISCELLANEOUS: "anything that does not clearly fit the categories above",
}

ALL_CATEGORIES = list(ExpenseCategory)

# Common alternative spellings a model (or a human) might produce.
_CATEGORY_ALIASES = {
    "food": ExpenseCategory.FOOD_MEALS,
    "meals": ExpenseCategory.FOOD_MEALS,
    "food and meals": ExpenseCategory.FOOD_MEALS,
    "food & beverage": ExpenseCategory.FOOD_MEALS,
    "hotel": ExpenseCategory.ACCOMMODATION,
    "lodging": ExpenseCategory.ACCOMMODATION,
    "transport": ExpenseCategory.TRANSPORTATION,
    "conveyance": ExpenseCategory.TRANSPORTATION,
    "travelling": ExpenseCategory.TRAVEL,
    "travelling expenses": ExpenseCategory.TRAVEL,
    "stationery": ExpenseCategory.OFFICE_SUPPLIES,
    "office": ExpenseCategory.OFFICE_SUPPLIES,
    "software": ExpenseCategory.SOFTWARE_SUBSCRIPTIONS,
    "subscriptions": ExpenseCategory.SOFTWARE_SUBSCRIPTIONS,
    "software and subscriptions": ExpenseCategory.SOFTWARE_SUBSCRIPTIONS,
    "professional": ExpenseCategory.PROFESSIONAL_SERVICES,
    "professional services": ExpenseCategory.PROFESSIONAL_SERVICES,
    "misc": ExpenseCategory.MISCELLANEOUS,
    "other": ExpenseCategory.MISCELLANEOUS,
    "others": ExpenseCategory.MISCELLANEOUS,
}


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
