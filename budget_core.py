"""Pure budget logic: splitting, redistribution, categories.

All money is integer cents. No function here mutates its input; each one
returns a new state or raises BudgetError with a message meant for the user.

The term total covers everything. Tuition is reserved off the top, and the
rest (the spending pool) is what the months split:

    total == sum(tuition amounts) + sum(month allocations)

A term runs from a first quarter to a last quarter. Fall, Winter and Spring
each carry one tuition payment; Summer has none. The plan can start partway
into its first quarter, and a payment's due date can fall anywhere, even
before the plan starts.

State shape:
    {
      "version": 3,
      "total": 1100000,                      # term total, cents
      "tuition": [{"id": "Fall 2026", "label": "Fall 2026", "quarter": "Fall 2026",
                   "due": "2026-09-17", "amount": 500000}],   # due may be None
      "months": [{"key": "2026-10", "alloc": 100000,
                  "locked": False, "overrides": {"c1": 30000},   # your amounts
                  "boost": {"c1": 5000},                         # optimizer's extra on top
                  "shift": 0}],      # allocation the optimizer moved in (+) or out (-); undone by un-optimize
      "categories": [{"id": "c1", "name": "Food", "default": 25000,
                      "static": False,   # static: amounts locked, optimizer skips it
                      "extra": False,    # extra: paid by money on top of the term total
                      "weight": 50}],    # optimizer share, 0-100
      "next_cat_id": 2,
      "items": [{"id": "i1", "name": "Tournament travel",     # one-off expenses,
                 "month": "2027-02", "amount": 22000}],       # inside a month
      "next_item_id": 2,
      "people": [{"id": "p1", "name": "Mom"}],             # who pays you
      "next_person_id": 2,
      "receipts": [{"id": "r1", "received": True,          # money in: a log only,
                    "date": "2026-10-01", "amount": 300000,  # it never moves budget
                    "from": "2026-10-01", "to": "2026-12-31",
                    "payer": "p1", "reimbursed": False, "reimbursed_date": None}],
      "next_receipt_id": 2
    }
"""

import copy
import datetime
import re
from decimal import Decimal

SCHEMA_VERSION = 9
DEFAULT_WEIGHT = 50  # optimizer slider, stored as 0-100 (shown as 0 to 1)
MAX_MONTHS = 60
MAX_CENTS = 100_000_000_00  # $100M, a sanity ceiling for typed input
MAX_NAME_LEN = 40
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# UCSC undergraduate registration fee due dates, 2026-27, from the
# Registrar's Academic & Administrative Calendar. Quarters not listed start
# with no due date; every date is editable in the app.
UCSC_DUE_DATES = {
    "Fall 2026": "2026-09-17",
    "Winter 2027": "2026-12-16",
    "Spring 2027": "2027-03-23",
}

# UCSC 2026-27 quarter dates (first and last day), from the Registrar.
UCSC_QUARTER_DATES = {
    "Fall 2026": ("2026-09-19", "2026-12-11"),
    "Winter 2027": ("2027-01-04", "2027-03-19"),
    "Spring 2027": ("2027-03-29", "2027-06-10"),
}

SEASONS = ["Winter", "Spring", "Summer", "Fall"]  # calendar order within a year
SEASON_MONTHS = {"Winter": (1, 3), "Spring": (4, 6), "Summer": (7, 8), "Fall": (9, 12)}
TUITION_SEASONS = {"Fall", "Winter", "Spring"}
MAX_QUARTERS = 16


class BudgetError(ValueError):
    """A change was rejected. The message explains why, in plain English."""


# ---------------------------------------------------------------- money

_MONEY_RE = re.compile(r"(\d+(\.\d{0,2})?|\.\d{1,2})")


def parse_money(text):
    """Parse a dollar string like "1,250.50" or "$80" into cents."""
    if not isinstance(text, str):
        raise BudgetError("Amounts must be sent as text, like 1250.50.")
    s = text.strip().replace(",", "").replace("$", "")
    if not _MONEY_RE.fullmatch(s):
        raise BudgetError(
            f'"{text.strip()}" is not a valid amount. Use a positive number '
            "with at most 2 decimals, like 1250 or 1250.50.")
    cents = int(Decimal(s) * 100)
    if cents > MAX_CENTS:
        raise BudgetError(f"{format_money(cents)} is larger than this tool allows.")
    return cents


def format_money(cents):
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


# ---------------------------------------------------------------- months

def _parse_month(key):
    m = re.fullmatch(r"(\d{4})-(\d{2})", key or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise BudgetError(f'"{key}" is not a month. Use the form YYYY-MM.')
    return int(m.group(1)), int(m.group(2))


def month_range(start, end):
    """Inclusive list of "YYYY-MM" keys from start to end."""
    y, m = _parse_month(start)
    ey, em = _parse_month(end)
    if (ey, em) < (y, m):
        raise BudgetError("The end month comes before the start month.")
    keys = []
    while (y, m) <= (ey, em):
        keys.append(f"{y:04d}-{m:02d}")
        if len(keys) > MAX_MONTHS:
            raise BudgetError(f"A term can be at most {MAX_MONTHS} months.")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys


def month_label(key):
    y, m = _parse_month(key)
    return f"{MONTH_NAMES[m - 1]} {y}"


# ---------------------------------------------------------------- splitting

def split_even(amount, n):
    """Split amount (may be negative) into n integer parts that sum to amount.

    Leftover cents go to the earliest parts, so [0] gets the extra first.
    """
    if n <= 0:
        raise ValueError("cannot split across zero parts")
    sign = -1 if amount < 0 else 1
    base, extra = divmod(abs(amount), n)
    return [sign * (base + (1 if i < extra else 0)) for i in range(n)]


def _parse_date(text):
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        raise BudgetError(f'"{text}" is not a date. Use the form YYYY-MM-DD.') from None


def date_label(text):
    d = _parse_date(text)
    return f"{MONTH_NAMES[d.month - 1]} {d.day}, {d.year}"


def tuition_total(state):
    return sum(p["amount"] for p in state["tuition"])


def spending_pool(state):
    """What's left for the months after tuition."""
    return state["total"] - tuition_total(state)


# ---------------------------------------------------------------- quarters

def _parse_quarter(label):
    m = re.fullmatch(r"(Winter|Spring|Summer|Fall) (\d{4})", label or "")
    if not m:
        raise BudgetError(f'"{label}" is not a quarter. Use a name like "Fall 2026".')
    return int(m.group(2)), SEASONS.index(m.group(1))


def _quarter_ord(label):
    y, i = _parse_quarter(label)
    return y * 4 + i


def _quarter_from_ord(o):
    return f"{SEASONS[o % 4]} {o // 4}"


def quarter_of(key):
    """Group a month into a quarter: Winter Jan-Mar, Spring Apr-Jun,
    Summer Jul-Aug, Fall Sep-Dec."""
    y, m = _parse_month(key)
    for season, (first, last) in SEASON_MONTHS.items():
        if first <= m <= last:
            return f"{season} {y}"


def quarter_months(label):
    y, i = _parse_quarter(label)
    first, last = SEASON_MONTHS[SEASONS[i]]
    return [f"{y:04d}-{m:02d}" for m in range(first, last + 1)]


def quarter_range(first, last):
    a, b = _quarter_ord(first), _quarter_ord(last)
    if b < a:
        raise BudgetError("The last quarter comes before the first quarter.")
    if b - a >= MAX_QUARTERS:
        raise BudgetError(f"A term can be at most {MAX_QUARTERS} quarters.")
    return [_quarter_from_ord(o) for o in range(a, b + 1)]


def has_tuition(quarter):
    return quarter.split()[0] in TUITION_SEASONS


def term_quarters(keys):
    out = []
    for k in keys:
        q = quarter_of(k)
        if not out or out[-1] != q:
            out.append(q)
    return out


def tuition_quarters(keys):
    return [q for q in term_quarters(keys) if has_tuition(q)]


def quarter_options(around="Fall 2026"):
    """Quarters offered in the pickers: a year before through two years after."""
    base = _quarter_ord(around)
    return [{"label": _quarter_from_ord(o), "months": quarter_months(_quarter_from_ord(o))}
            for o in range(base - 4, base + 9)]


def term_months(first_quarter, last_quarter, start=None):
    """Month keys for a term: from start (a month in the first quarter,
    defaulting to its first month) through the end of the last quarter."""
    quarters = quarter_range(first_quarter, last_quarter)
    first_months = quarter_months(quarters[0])
    start = start or first_months[0]
    if start not in first_months:
        raise BudgetError(
            f"The plan has to start in a month of {quarters[0]} "
            f"({month_label(first_months[0])} to {month_label(first_months[-1])}).")
    return month_range(start, quarter_months(quarters[-1])[-1])


def _payment(quarter, amount):
    return {"id": quarter, "label": quarter, "quarter": quarter,
            "due": UCSC_DUE_DATES.get(quarter), "amount": amount}


def new_budget_from_months(total, start, end, tuition_amount=0):
    """Create a budget over explicit months. Each Fall, Winter and Spring
    quarter touched gets one tuition payment of tuition_amount; the rest of
    the total is split evenly across the months."""
    if not isinstance(total, int) or total <= 0:
        raise BudgetError("The term total must be more than $0.00.")
    keys = month_range(start, end)
    tuition = [_payment(q, tuition_amount) for q in tuition_quarters(keys)]
    pool = total - tuition_amount * len(tuition)
    if pool < 0:
        raise BudgetError(
            f"Tuition ({len(tuition)} x {format_money(tuition_amount)} = "
            f"{format_money(total - pool)}) is more than the term total "
            f"of {format_money(total)}.")
    allocs = split_even(pool, len(keys))
    return {
        "version": SCHEMA_VERSION,
        "total": total,
        "tuition": tuition,
        "months": [{"key": k, "alloc": a, "locked": False, "overrides": {}, "boost": {}, "shift": 0}
                   for k, a in zip(keys, allocs)],
        "categories": [],
        "next_cat_id": 1,
        "items": [],
        "next_item_id": 1,
        "people": [],
        "next_person_id": 1,
        "receipts": [],
        "next_receipt_id": 1,
    }


def new_budget(total, first_quarter, last_quarter, start=None, tuition_amount=0):
    keys = term_months(first_quarter, last_quarter, start)
    return new_budget_from_months(total, keys[0], keys[-1], tuition_amount)


# ---------------------------------------------------------------- categories

def base_amount(month, cat):
    """The amount you set: the month's override, or the category default."""
    return month["overrides"].get(cat["id"], cat["default"])


def boost_amount(month, cat):
    """Extra the optimizer added on top. Re-optimizing can raise or lower it,
    but never takes the category below base_amount."""
    return month["boost"].get(cat["id"], 0)


def category_amount(state, month, cat):
    return base_amount(month, cat) + boost_amount(month, cat)


def category_total(state, index):
    month = state["months"][index]
    return sum(category_amount(state, month, c) for c in state["categories"])


def items_in(state, key):
    return [it for it in state["items"] if it["month"] == key]


def extra_amount(state, index):
    """This month's amount for "adds to total" categories (like gas). That money
    comes on top of the term total, so it adds to the month instead of using it up."""
    month = state["months"][index]
    return sum(category_amount(state, month, c) for c in state["categories"] if c["extra"])


def soft_boost(state, index):
    """Optimizer extra on dynamic categories in a month: money that re-optimizing
    can take back, so it doesn't count as a hard floor."""
    month = state["months"][index]
    return sum(boost_amount(month, c) for c in dynamic_categories(state))


def committed(state, index):
    """What a month's allocation already has to cover: its categories (except
    the "adds to total" ones, which bring their own money) plus one-off expenses.
    Redistribution can't push an allocation below this."""
    key = state["months"][index]["key"]
    return (category_total(state, index) - extra_amount(state, index)
            + sum(it["amount"] for it in items_in(state, key)))


def _check_index(state, index):
    if not isinstance(index, int) or isinstance(index, bool) \
            or not 0 <= index < len(state["months"]):
        raise BudgetError("That month does not exist.")


def _find_cat(state, cat_id):
    for c in state["categories"]:
        if c["id"] == cat_id:
            return c
    raise BudgetError("That category does not exist.")


def _clean_name(state, name, ignore_id=None):
    if not isinstance(name, str) or not name.strip():
        raise BudgetError("Category names can't be blank.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LEN:
        raise BudgetError(f"Keep category names under {MAX_NAME_LEN} characters.")
    for c in state["categories"]:
        if c["id"] != ignore_id and c["name"].lower() == name.lower():
            raise BudgetError(f'There is already a category called "{c["name"]}".')
    return name


def add_category(state, name, default):
    s = copy.deepcopy(state)
    name = _clean_name(s, name)
    cat_id = f"c{s['next_cat_id']}"
    s["next_cat_id"] += 1
    s["categories"].append({"id": cat_id, "name": name, "default": default,
                            "static": False, "extra": False, "weight": DEFAULT_WEIGHT})
    return s


def rename_category(state, cat_id, name):
    s = copy.deepcopy(state)
    _find_cat(s, cat_id)["name"] = _clean_name(s, name, ignore_id=cat_id)
    return s


def _not_static(cat, doing):
    if cat["static"]:
        raise BudgetError(f'{cat["name"]} is static, so it can\'t be {doing}. '
                          "Uncheck Static on it first.")


def set_category_default(state, cat_id, default):
    s = copy.deepcopy(state)
    cat = _find_cat(s, cat_id)
    _not_static(cat, "changed")
    cat["default"] = default
    return s


def set_category_extra(state, cat_id, extra):
    """Mark a category as paid by money on top of the term total."""
    s = copy.deepcopy(state)
    _find_cat(s, cat_id)["extra"] = bool(extra)
    return s


def set_category_static(state, cat_id, static):
    s = copy.deepcopy(state)
    _find_cat(s, cat_id)["static"] = bool(static)
    return s


def set_category_weight(state, cat_id, weight):
    """How strongly the optimizer favors this category, 0-100 (0 to 1 on screen)."""
    if not isinstance(weight, int) or isinstance(weight, bool) or not 0 <= weight <= 100:
        raise BudgetError("The slider has to be between 0 and 1.")
    s = copy.deepcopy(state)
    _find_cat(s, cat_id)["weight"] = weight
    return s


def delete_category(state, cat_id):
    s = copy.deepcopy(state)
    _not_static(_find_cat(s, cat_id), "deleted")
    s["categories"] = [c for c in s["categories"] if c["id"] != cat_id]
    for m in s["months"]:
        m["overrides"].pop(cat_id, None)
        m["boost"].pop(cat_id, None)
    return s


def set_override(state, index, cat_id, cents):
    """Set one month's amount for a category. cents=None goes back to default."""
    s = copy.deepcopy(state)
    _check_index(s, index)
    _not_static(_find_cat(s, cat_id), "changed")
    overrides = s["months"][index]["overrides"]
    if cents is None:
        overrides.pop(cat_id, None)
    else:
        overrides[cat_id] = cents
    return s


# ---------------------------------------------------------------- individual expenses

def _find_item(state, item_id):
    for it in state["items"]:
        if it["id"] == item_id:
            return it
    raise BudgetError("That expense does not exist.")


def _clean_item_name(name):
    if not isinstance(name, str) or not name.strip():
        raise BudgetError("Give the expense a name.")
    name = " ".join(name.split())
    if len(name) > 60:
        raise BudgetError("Keep expense names under 60 characters.")
    return name


def _check_item_month(state, key):
    if key not in [m["key"] for m in state["months"]]:
        raise BudgetError("Pick a month inside the term for this expense.")


def add_item(state, name, month, amount):
    """Add a one-off expense to a month. Like a category, it uses up part of
    that month's allocation; nothing moves between months."""
    s = copy.deepcopy(state)
    name = _clean_item_name(name)
    _check_item_month(s, month)
    item_id = f"i{s['next_item_id']}"
    s["next_item_id"] += 1
    s["items"].append({"id": item_id, "name": name, "month": month, "amount": amount})
    return s


def update_item(state, item_id, name=None, month=None, amount=None):
    s = copy.deepcopy(state)
    it = _find_item(s, item_id)
    if name is not None:
        it["name"] = _clean_item_name(name)
    if month is not None:
        _check_item_month(s, month)
        it["month"] = month
    if amount is not None:
        it["amount"] = amount
    return s


def delete_item(state, item_id):
    s = copy.deepcopy(state)
    _find_item(s, item_id)
    s["items"] = [it for it in s["items"] if it["id"] != item_id]
    return s


# ---------------------------------------------------------------- months

def set_locked(state, index, locked):
    s = copy.deepcopy(state)
    _check_index(s, index)
    s["months"][index]["locked"] = bool(locked)
    return s


def _spread(state, amount, exclude=None):
    """Plan giving `amount` (negative means taking) evenly to every unlocked
    month except `exclude`, without applying it.

    Returns (others, shares, problems). problems lists (index, after, floor)
    for every month that would end up below zero or what's planned in it.
    """
    months = state["months"]
    others = [i for i, m in enumerate(months) if i != exclude and not m["locked"]]
    if not others:
        return others, [], []
    shares = split_even(amount, len(others))
    problems = []
    for i, share in zip(others, shares):
        if share >= 0:
            continue  # receiving money never makes a month worse
        after = months[i]["alloc"] + share
        floor = max(0, committed(state, i) - soft_boost(state, i))
        if after < floor:
            problems.append((i, after, floor))
    return others, shares, problems


def _describe(state, problems):
    names = []
    for i, after, floor in problems[:3]:
        what = "$0.00" if floor == 0 else f"what's planned in it ({format_money(floor)})"
        names.append(f"{month_label(state['months'][i]['key'])} would drop to "
                     f"{format_money(after)}, below {what}")
    more = f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""
    return "; ".join(names) + more


def max_takeable(state, exclude=None):
    """The most that can be taken evenly from the unlocked months (except
    `exclude`) before one drops below $0 or what's planned in it."""
    def ok(x):
        others, _, problems = _spread(state, -x, exclude)
        return bool(others) and not problems
    lo, hi = 0, spending_pool(state)
    while lo < hi:  # feasibility only gets worse as more is taken
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _take_or_explain(state, amount, exclude, attempt, best, hint):
    """Plan moving `amount` into the unlocked months (negative takes from
    them). Raises a BudgetError that explains why if it can't be done."""
    others, shares, problems = _spread(state, amount, exclude)
    if not others:
        raise BudgetError(
            f"{attempt} needs at least one unlocked month to absorb the "
            "difference, and they're all locked. Unlock a month first.")
    if problems:
        plural = "s" if len(others) != 1 else ""
        raise BudgetError(
            f"{attempt} isn't possible. It takes {format_money(-amount)} "
            f"out of spending, split across {len(others)} unlocked month{plural}, "
            f"and {_describe(state, problems)}. {best()} {hint}")
    return others, shares


_HINT = "Lower categories or expenses, or unlock more months, to free up room."


def _apply(state, others, shares):
    for i, share in zip(others, shares):
        state["months"][i]["alloc"] += share


def _assert_balanced(state):
    assert tuition_total(state) + sum(m["alloc"] for m in state["months"]) == state["total"]


# ---------------------------------------------------------------- month edits

def max_allocation(state, index):
    """The highest amount this month can be set to without blocking."""
    _check_index(state, index)
    return state["months"][index]["alloc"] + max_takeable(state, exclude=index)


def set_month_allocation(state, index, new_alloc):
    """Change one month's allocation, spreading the difference evenly across
    the other unlocked months. The term total never changes."""
    _check_index(state, index)
    months = state["months"]
    label = month_label(months[index]["key"])
    if not isinstance(new_alloc, int) or new_alloc < 0:
        raise BudgetError("A month can't be set below $0.00.")
    pool = spending_pool(state)
    if new_alloc > pool:
        raise BudgetError(
            f"{format_money(new_alloc)} is more than all the spending money in "
            f"the term ({format_money(pool)} after tuition).")
    delta = new_alloc - months[index]["alloc"]
    if delta == 0:
        return copy.deepcopy(state)
    others, shares = _take_or_explain(
        state, -delta, index, f"Setting {label} to {format_money(new_alloc)}",
        lambda: f"The most {label} can be right now is "
                f"{format_money(max_allocation(state, index))}.", _HINT)
    s = copy.deepcopy(state)
    s["months"][index]["alloc"] = new_alloc
    _apply(s, others, shares)
    _assert_balanced(s)
    return s


# ---------------------------------------------------------------- term edits

def min_total(state):
    """The lowest the term total can go: tuition stays, months give up what they can."""
    return state["total"] - max_takeable(state)


def set_total(state, new_total):
    """Change the term total. The difference goes to, or comes from, the
    unlocked months evenly. Tuition doesn't change."""
    if not isinstance(new_total, int) or new_total <= 0:
        raise BudgetError("The term total must be more than $0.00.")
    delta = new_total - state["total"]
    if delta == 0:
        return copy.deepcopy(state)
    others, shares = _take_or_explain(
        state, delta, None, f"Lowering the term total to {format_money(new_total)}",
        lambda: f"The lowest it can go right now is {format_money(min_total(state))}.",
        _HINT)
    s = copy.deepcopy(state)
    s["total"] = new_total
    _apply(s, others, shares)
    _assert_balanced(s)
    return s


def set_term_quarters(state, first_quarter, last_quarter, start=None):
    """Change which quarters the term covers and the month planning starts.

    Months that stay keep their categories and lock; new months start fresh.
    Quarters that stay keep their tuition; new Fall/Winter/Spring quarters get
    the same per-quarter amount as the others (or $0 if they differ). Spending
    is then re-split evenly across the unlocked months, and locked months keep
    their amounts."""
    keys = term_months(first_quarter, last_quarter, start)
    stranded = [it for it in state["items"] if it["month"] not in keys]
    if stranded:
        names = ", ".join(f'{it["name"]} ({month_label(it["month"])})' for it in stranded[:3])
        more = f" and {len(stranded) - 3} more" if len(stranded) > 3 else ""
        raise BudgetError(
            f"Some individual expenses are in months the new term leaves out: "
            f"{names}{more}. Move them to another month or delete them first.")
    old_pay = {p["quarter"]: p for p in state["tuition"]}
    amounts = {p["amount"] for p in state["tuition"]}
    fill = amounts.pop() if len(amounts) == 1 else 0
    tuition = [copy.deepcopy(old_pay[q]) if q in old_pay else _payment(q, fill)
               for q in tuition_quarters(keys)]
    t_total = sum(p["amount"] for p in tuition)
    pool = state["total"] - t_total
    if pool < 0:
        raise BudgetError(
            f"Tuition for {len(tuition)} quarters would be {format_money(t_total)}, "
            f"more than the term total of {format_money(state['total'])}. "
            "Raise the term total or lower tuition first.")

    old = {m["key"]: m for m in state["months"]}
    months = [copy.deepcopy(old[k]) if k in old else
              {"key": k, "alloc": 0, "locked": False, "overrides": {}, "boost": {}, "shift": 0} for k in keys]
    locked = [m for m in months if m["locked"]]
    unlocked = [m for m in months if not m["locked"]]
    locked_sum = sum(m["alloc"] for m in locked)
    if not unlocked:
        raise BudgetError("Every month in the new term is locked, so there's "
                          "nowhere to put the spending money. Unlock a month first.")
    if locked_sum > pool:
        raise BudgetError(
            f"The locked months in the new term add up to {format_money(locked_sum)}, "
            f"more than the {format_money(pool)} of spending money. Unlock some months first.")
    free = pool - locked_sum
    for m, a in zip(unlocked, split_even(free, len(unlocked))):
        m["alloc"] = a
    s = copy.deepcopy(state)
    for m in months:
        m["shift"] = 0
    s["months"] = months
    s["tuition"] = tuition
    short = [(i, m["alloc"], committed(s, i)) for i, m in enumerate(months)
             if not m["locked"] and m["alloc"] < committed(s, i)]
    if short:
        raise BudgetError(
            f"Re-splitting {format_money(free)} across {len(unlocked)} unlocked "
            f"months leaves too little: {_describe(s, short)}. {_HINT}")
    _assert_balanced(s)
    return s


# ---------------------------------------------------------------- tuition

def _find_payment(state, pid):
    for p in state["tuition"]:
        if p["id"] == pid:
            return p
    raise BudgetError("That tuition payment does not exist.")


def _set_tuition_amounts(state, amounts, attempt, best):
    """amounts maps payment id -> new cents. The difference comes out of, or
    goes back to, the unlocked months evenly."""
    if any(not isinstance(a, int) or a < 0 for a in amounts.values()):
        raise BudgetError("Tuition can't be below $0.00.")
    delta = sum(a - _find_payment(state, pid)["amount"] for pid, a in amounts.items())
    if delta == 0:
        return copy.deepcopy(state)
    others, shares = _take_or_explain(state, -delta, None, attempt, best, _HINT)
    s = copy.deepcopy(state)
    for pid, a in amounts.items():
        _find_payment(s, pid)["amount"] = a
    _apply(s, others, shares)
    _assert_balanced(s)
    return s


def _require_tuition(state):
    if not state["tuition"]:
        raise BudgetError("This budget has no tuition payments yet. Add them first.")


def max_tuition_each(state):
    return (tuition_total(state) + max_takeable(state)) // len(state["tuition"])


def max_tuition_payment(state, pid):
    return _find_payment(state, pid)["amount"] + max_takeable(state)


def set_tuition(state, amount, pid=None):
    """Set one payment (pid) or every payment (pid=None) to amount."""
    _require_tuition(state)
    if pid is None:
        return _set_tuition_amounts(
            state, {p["id"]: amount for p in state["tuition"]},
            f"Setting tuition to {format_money(amount)} per quarter",
            lambda: f"The most it can be right now is "
                    f"{format_money(max_tuition_each(state))} per quarter.")
    label = _find_payment(state, pid)["label"]
    return _set_tuition_amounts(
        state, {pid: amount},
        f"Setting {label} tuition to {format_money(amount)}",
        lambda: f"The most it can be right now is "
                f"{format_money(max_tuition_payment(state, pid))}.")


def set_tuition_due(state, pid, due):
    s = copy.deepcopy(state)
    p = _find_payment(s, pid)
    if due in (None, ""):
        p["due"] = None
    else:
        _parse_date(due)
        p["due"] = due
    return s


# ---------------------------------------------------------------- optimizer

def weighted_split(amount, weights):
    """Split amount (>= 0) in proportion to integer weights. Whole cents only:
    leftover cents go to the largest fractional shares, ties to the earlier one."""
    total_w = sum(weights)
    if amount <= 0 or total_w <= 0:
        return [0] * len(weights)
    parts = [amount * w // total_w for w in weights]
    leftover = amount - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % total_w), i))
    for i in order[:leftover]:
        parts[i] += 1
    return parts


def dynamic_categories(state):
    return [c for c in state["categories"] if not c["static"] and not c["extra"]]


def optimize_plan(state):
    """Work out the optimizer's extra for every month, from scratch.

    A month's "free" money is its allocation minus everything planned in it
    except the dynamic categories' current extra. Within each quarter, the
    unlocked months pool their free money and split it evenly, so every month
    in the quarter gets the same extra. To make that fit, allocation shifts
    between those months (never across quarters, never in locked months):
    each unlocked month ends at exactly its own amounts plus its even share.
    Locked months just get their own free money. The even share is then split
    across the dynamic categories by slider.

    If a quarter's pooled free money is $0 or less, its months get no extra and
    keep their allocations; any that are over stay over, because fixing it
    would mean dropping below a budget. Nothing is changed here."""
    dyn = dynamic_categories(state)
    active = [c for c in dyn if c["weight"] > 0]
    months = state["months"]
    free = [m["alloc"] - committed(state, i) + soft_boost(state, i) for i, m in enumerate(months)]
    target = list(free)                       # extra each month will get
    alloc = [m["alloc"] for m in months]      # allocation after evening out

    groups = {}
    for i, m in enumerate(months):
        groups.setdefault(quarter_of(m["key"]), []).append(i)
    for idx in groups.values():
        unlocked = [i for i in idx if not months[i]["locked"]]
        pooled = sum(free[i] for i in unlocked)
        if not unlocked:
            continue
        if pooled > 0:
            for i, share in zip(unlocked, split_even(pooled, len(unlocked))):
                alloc[i] = months[i]["alloc"] - free[i] + share
                target[i] = share
        else:  # the quarter is short overall: no extra anywhere in it
            for i in unlocked:
                target[i] = 0
    plan = []
    for i, m in enumerate(months):
        extra = max(target[i], 0) if active else 0
        if alloc[i] != m["alloc"] and not active:
            alloc[i] = m["alloc"]
        parts = weighted_split(extra, [c["weight"] for c in active])
        new = {c["id"]: 0 for c in dyn}
        new.update({c["id"]: p for c, p in zip(active, parts)})
        plan.append({"index": i, "free": free[i], "shares": new,
                     "current": {c["id"]: boost_amount(m, c) for c in dyn},
                     "alloc": m["alloc"], "alloc_new": alloc[i]})
    return plan


def apply_optimize(state):
    """Set each dynamic category's extra (and even out allocations within each
    quarter) to the plan. This both adds unused money and takes extra back from
    months that went over."""
    if not dynamic_categories(state):
        raise BudgetError("Every category is static. Uncheck Static on at least one "
                          "so the optimizer has somewhere to put money.")
    plan = optimize_plan(state)
    if all(row["shares"] == row["current"] and row["alloc_new"] == row["alloc"] for row in plan):
        raise BudgetError("Already optimized. Nothing would change.")
    if not any(c["weight"] > 0 for c in dynamic_categories(state)) \
            and not any(any(row["current"].values()) for row in plan):
        raise BudgetError("Every dynamic category's slider is at 0. Move at least one above 0.")
    s = copy.deepcopy(state)
    for row in plan:
        m = s["months"][row["index"]]
        m["shift"] += row["alloc_new"] - m["alloc"]   # remembered so un-optimize can undo it
        m["alloc"] = row["alloc_new"]
        for cat_id, amount in row["shares"].items():
            if amount:
                m["boost"][cat_id] = amount
            else:
                m["boost"].pop(cat_id, None)
    _assert_balanced(s)
    return s


def clear_optimize(state):
    """Un-optimize: remove all of Optimize's extra and move back every bit of
    allocation it shifted between months, so each month is exactly where it
    was before optimizing. Re-optimizing can rebuild it any time."""
    if not any(m["boost"] or m["shift"] for m in state["months"]):
        raise BudgetError("Nothing to undo. Optimize hasn't changed anything.")
    s = copy.deepcopy(state)
    for m in s["months"]:
        if m["alloc"] - m["shift"] < 0:
            raise BudgetError(
                f"Can't undo Optimize: {month_label(m['key'])} would drop below $0.00 "
                "because its allocation was lowered after optimizing. Raise it first.")
        m["alloc"] -= m["shift"]
        m["shift"] = 0
        m["boost"] = {}
    _assert_balanced(s)
    return s


# ---------------------------------------------------------------- dates and coverage

def _month_bounds(key):
    y, m = _parse_month(key)
    first = datetime.date(y, m, 1)
    nxt = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    return first, nxt - datetime.timedelta(days=1)


def _prorated(alloc, key, start, end):
    """The part of a month's allocation that falls between start and end
    (inclusive), by days. Rounded down to the cent."""
    first, last = _month_bounds(key)
    lo, hi = max(first, start), min(last, end)
    if hi < lo:
        return 0
    return alloc * ((hi - lo).days + 1) // ((last - first).days + 1)


def need_for_range(state, start, end):
    """What the plan needs between two dates: each month's allocation scaled
    to the days covered, plus tuition due in the range."""
    a, b = _parse_date(start), _parse_date(end)
    if b < a:
        raise BudgetError("The end date comes before the start date.")
    spending = sum(_prorated(m["alloc"] + extra_amount(state, i), m["key"], a, b)
                   for i, m in enumerate(state["months"]))
    extra = sum(_prorated(extra_amount(state, i), m["key"], a, b)
                for i, m in enumerate(state["months"]))
    tuition = sum(p["amount"] for p in state["tuition"]
                  if p["due"] and a <= _parse_date(p["due"]) <= b)
    plan_first = _month_bounds(state["months"][0]["key"])[0]
    plan_last = _month_bounds(state["months"][-1]["key"])[1]
    return {"spending": spending, "extra": extra, "tuition": tuition, "total": spending + tuition,
            "outside": a < plan_first or b > plan_last,
            "label": f"{date_label(start)} to {date_label(end)}"}


def quarter_left(state, quarter, on):
    """How much of a quarter's plan is still ahead as of a date: the months'
    allocations from that day to the end of the quarter, plus its tuition if
    it isn't due yet."""
    d = _parse_date(on)
    months = [dict(m, alloc=m["alloc"] + extra_amount(state, i))
              for i, m in enumerate(state["months"]) if quarter_of(m["key"]) == quarter]
    if not months:
        raise BudgetError(f"{quarter} isn't in this term.")
    end = _month_bounds(months[-1]["key"])[1]
    spending = sum(_prorated(m["alloc"], m["key"], d, end) for m in months)
    total_spending = sum(m["alloc"] for m in months)
    pay = next((p for p in state["tuition"] if p["quarter"] == quarter), None)
    tuition = pay["amount"] if pay and pay["due"] and _parse_date(pay["due"]) >= d else 0
    return {"spending": spending, "of": total_spending, "tuition": tuition,
            "total": spending + tuition, "ended": d > end,
            "start": _month_bounds(months[0]["key"])[0].isoformat(), "end": end.isoformat()}


# ---------------------------------------------------------------- people and money in

def _find_person(state, pid):
    for p in state["people"]:
        if p["id"] == pid:
            return p
    raise BudgetError("That person does not exist.")


def _clean_person(state, name, ignore_id=None):
    if not isinstance(name, str) or not name.strip():
        raise BudgetError("Names can't be blank.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LEN:
        raise BudgetError(f"Keep names under {MAX_NAME_LEN} characters.")
    for p in state["people"]:
        if p["id"] != ignore_id and p["name"].lower() == name.lower():
            raise BudgetError(f'"{p["name"]}" is already on the list.')
    return name


def add_person(state, name):
    s = copy.deepcopy(state)
    name = _clean_person(s, name)
    s["people"].append({"id": f"p{s['next_person_id']}", "name": name})
    s["next_person_id"] += 1
    return s


def rename_person(state, pid, name):
    s = copy.deepcopy(state)
    _find_person(s, pid)["name"] = _clean_person(s, name, ignore_id=pid)
    return s


def delete_person(state, pid):
    s = copy.deepcopy(state)
    person = _find_person(s, pid)
    used = sum(1 for r in s["receipts"] if r["payer"] == pid)
    if used:
        raise BudgetError(f'{person["name"]} is listed on {used} payment{"s" if used != 1 else ""}. '
                          "Change who paid on those first.")
    s["people"] = [p for p in s["people"] if p["id"] != pid]
    return s


def _find_receipt(state, rid):
    for r in state["receipts"]:
        if r["id"] == rid:
            return r
    raise BudgetError("That payment does not exist.")


_RECEIPT_FIELDS = ("received", "date", "amount", "from", "to", "payer",
                   "reimbursed", "reimbursed_date")


def _check_receipt(state, r):
    for key in ("date", "from", "to", "reimbursed_date"):
        if r[key] in ("", None):
            r[key] = None
        else:
            _parse_date(r[key])
    if not r["from"] or not r["to"]:
        raise BudgetError("Say what dates this money is for (from and to).")
    if r["to"] < r["from"]:
        raise BudgetError("The 'to' date comes before the 'from' date.")
    if r["received"] and not r["date"]:
        raise BudgetError("Add the date you received it.")
    if not isinstance(r["amount"], int) or r["amount"] <= 0:
        raise BudgetError("The amount has to be more than $0.00.")
    if r["payer"] in ("", None):
        r["payer"] = None
    else:
        _find_person(state, r["payer"])
    r["received"] = bool(r["received"])
    r["reimbursed"] = bool(r["reimbursed"])
    if not r["reimbursed"]:
        r["reimbursed_date"] = None


def add_receipt(state, **fields):
    s = copy.deepcopy(state)
    r = {"id": f"r{s['next_receipt_id']}", "received": True, "date": None, "amount": 0,
         "from": None, "to": None, "payer": None, "reimbursed": False, "reimbursed_date": None}
    r.update({k: v for k, v in fields.items() if k in _RECEIPT_FIELDS})
    _check_receipt(s, r)
    s["receipts"].append(r)
    s["next_receipt_id"] += 1
    return s


def update_receipt(state, rid, **fields):
    s = copy.deepcopy(state)
    r = _find_receipt(s, rid)
    r.update({k: v for k, v in fields.items() if k in _RECEIPT_FIELDS})
    _check_receipt(s, r)
    return s


def delete_receipt(state, rid):
    s = copy.deepcopy(state)
    _find_receipt(s, rid)
    s["receipts"] = [r for r in s["receipts"] if r["id"] != rid]
    return s


def coverage(state, amount, start, end):
    need = need_for_range(state, start, end)
    return dict(need, amount=amount, diff=amount - need["total"])


def period_ends(state, as_of):
    """Whole periods you can ask about, from as_of: the end of each month and
    each quarter in the term that hasn't ended yet, plus the end of the term."""
    d = _parse_date(as_of)
    out, seen_q = [], set()
    for m in state["months"]:
        last = _month_bounds(m["key"])[1]
        if last >= d:
            out.append({"date": last.isoformat(), "label": f"End of {month_label(m['key'])}", "kind": "month"})
        q = quarter_of(m["key"])
        q_months = [x for x in state["months"] if quarter_of(x["key"]) == q]
        q_last = _month_bounds(q_months[-1]["key"])[1]
        if q not in seen_q and q_last >= d:
            seen_q.add(q)
            out.append({"date": q_last.isoformat(), "label": f"End of {q}", "kind": "quarter"})
    if out:
        out.append({"date": out[-1]["date"], "label": "End of the term", "kind": "term"})
    order = {"month": 0, "quarter": 1, "term": 2}
    return sorted(out, key=lambda p: (p["date"], order[p["kind"]]))


def need_parts(state, start, end, exclude=()):
    """What the plan needs between two dates, split into parts you can leave
    out of a request: tuition due in the range, each category, individual
    expenses, and unallocated money. Month amounts are scaled to the days
    covered. Returns (parts, total of the included parts)."""
    a, b = _parse_date(start), _parse_date(end)
    if b < a:
        raise BudgetError("The end date comes before the start date.")
    exclude = set(exclude)
    amounts = {"tuition": sum(p["amount"] for p in state["tuition"]
                              if p["due"] and a <= _parse_date(p["due"]) <= b)}
    for c in state["categories"]:
        amounts[f"cat:{c['id']}"] = 0
    amounts["items"] = amounts["free"] = 0
    for i, m in enumerate(state["months"]):
        def part(x, key=m["key"]):
            return _prorated(x, key, a, b)
        for c in state["categories"]:
            amounts[f"cat:{c['id']}"] += part(category_amount(state, m, c))
        amounts["items"] += part(sum(it["amount"] for it in items_in(state, m["key"])))
        used = category_total(state, i) + sum(it["amount"] for it in items_in(state, m["key"]))
        amounts["free"] += part(max(m["alloc"] + extra_amount(state, i) - used, 0))
    labels = [("tuition", "Tuition", "tuition")]
    labels += [(f"cat:{c['id']}", c["name"], "extra" if c["extra"] else "category") for c in state["categories"]]
    labels += [("items", "Individual expenses", "items"), ("free", "Unallocated money", "free")]
    parts = [{"key": k, "label": lab, "kind": kind, "amount": amounts[k], "included": k not in exclude}
             for k, lab, kind in labels]
    return parts, sum(p["amount"] for p in parts if p["included"])


def period_starts(state, today):
    """Whole periods a request can start from: the first day of each month and
    each quarter in the term that hasn't ended by today. A quarter starts at
    its first month in the term (so a plan starting in October starts Fall on
    Oct 1)."""
    d = _parse_date(today)
    out, seen_q = [], set()
    for m in state["months"]:
        first, last = _month_bounds(m["key"])
        q = quarter_of(m["key"])
        q_months = [x for x in state["months"] if quarter_of(x["key"]) == q]
        q_last = _month_bounds(q_months[-1]["key"])[1]
        if q not in seen_q and q_last >= d:
            seen_q.add(q)
            out.append({"date": first.isoformat(), "label": f"Start of {q}", "kind": "quarter",
                        "current": first <= d})
        if last >= d:
            out.append({"date": first.isoformat(), "label": f"Start of {month_label(m['key'])}",
                        "kind": "month", "current": first <= d <= last})
    return sorted(out, key=lambda p: (p["date"], p["kind"] != "quarter"))


def request_quote(state, as_of, through, include_expected=False, exclude=()):
    """How much to ask for, from as_of through the end of a whole period.

    Each payment in the log covers its "for" dates evenly by day, so what's
    left of it is its amount times the share of its days that fall in the
    window. The request is what the plan needs in the window minus what's left
    of those payments. Also reports how far payments cover without a gap
    ("paid through") and the dates nothing covers."""
    a, b = _parse_date(as_of), _parse_date(through)
    if b < a:
        raise BudgetError("Pick a period that ends after the 'as of' date.")
    parts, need_total = need_parts(state, as_of, through, exclude)
    need = {"label": f"{date_label(as_of)} to {date_label(through)}", "total": need_total,
            "parts": parts, "excluded": sum(p["amount"] for p in parts if not p["included"])}
    counted = [r for r in state["receipts"] if r["received"] or include_expected]
    names = {p["id"]: p["name"] for p in state["people"]}
    parts, ranges = [], []
    for r in counted:
        f, t = _parse_date(r["from"]), _parse_date(r["to"])
        lo, hi = max(f, a), min(t, b)
        if hi < lo:
            continue
        days, total_days = (hi - lo).days + 1, (t - f).days + 1
        parts.append({"id": r["id"], "amount": r["amount"], "received": r["received"],
                      "payer": names.get(r["payer"]), "from": r["from"], "to": r["to"],
                      "days": days, "of_days": total_days,
                      "left": r["amount"] * days // total_days})
        ranges.append((lo, hi))
    covered = sum(p["left"] for p in parts)

    # walk forward from as_of through every day some payment covers
    ranges.sort()
    paid_through, cursor, gaps = None, a, []
    for lo, hi in ranges:
        if lo > cursor:
            gaps.append((cursor, lo - datetime.timedelta(days=1)))
        if hi >= cursor:
            cursor = hi + datetime.timedelta(days=1)
            if not gaps:
                paid_through = hi
    if cursor <= b:
        gaps.append((cursor, b))
    gap_rows = []
    for lo, hi in gaps:
        _, n = need_parts(state, lo.isoformat(), hi.isoformat(), exclude)
        gap_rows.append({"from": lo.isoformat(), "to": hi.isoformat(),
                         "label": f"{date_label(lo.isoformat())} to {date_label(hi.isoformat())}", "need": n})
    return {
        "as_of": as_of, "through": through, "need": need, "payments": parts,
        "covered": covered, "request": max(need["total"] - covered, 0),
        "extra": max(covered - need["total"], 0),
        "paid_through": paid_through.isoformat() if paid_through else None,
        "paid_through_label": date_label(paid_through.isoformat()) if paid_through else None,
        "gaps": gap_rows,
    }


def _paid_view(state):
    names = {p["id"]: p["name"] for p in state["people"]}
    receipts = []
    for r in sorted(state["receipts"], key=lambda r: (r["from"], r["id"])):
        receipts.append(dict(r, payer_name=names.get(r["payer"]),
                             check=coverage(state, r["amount"], r["from"], r["to"])))
    got = sum(r["amount"] for r in state["receipts"] if r["received"])
    expected = sum(r["amount"] for r in state["receipts"] if not r["received"])
    owed = sum(r["amount"] for r in state["receipts"] if r["payer"] and not r["reimbursed"])
    return {"people": [dict(p, uses=sum(1 for r in state["receipts"] if r["payer"] == p["id"]))
                       for p in state["people"]],
            "receipts": receipts, "received": got, "expected": expected,
            "not_reimbursed": owed}


# ---------------------------------------------------------------- views

def summarize(state):
    """Everything the UI shows, precomputed so the browser does no money math."""
    first_key = state["months"][0]["key"]
    tuition = [dict(p, due_label=date_label(p["due"]) if p["due"] else None,
                    before_start=bool(p["due"]) and p["due"][:7] < first_key)
               for p in state["tuition"]]
    takeable = max_takeable(state)
    months = []
    for i, m in enumerate(state["months"]):
        cats = [{"id": c["id"], "name": c["name"],
                 "amount": category_amount(state, m, c),
                 "base": base_amount(m, c), "boost": boost_amount(m, c),
                 "overridden": c["id"] in m["overrides"]}
                for c in state["categories"]]
        cat_total = sum(c["amount"] for c in cats)
        items = [dict(it) for it in items_in(state, m["key"])]
        items_total = sum(it["amount"] for it in items)
        used = cat_total + items_total
        due = [p for p in tuition if p["due"] and p["due"][:7] == m["key"]]
        months.append({
            "index": i, "key": m["key"], "label": month_label(m["key"]),
            "alloc": m["alloc"] + extra_amount(state, i), "alloc_base": m["alloc"],
            "extra": extra_amount(state, i), "locked": m["locked"],
            "categories": cats, "cat_total": cat_total,
            "items": items, "items_total": items_total, "used": used,
            "remainder": m["alloc"] + extra_amount(state, i) - used,
            "over": used > m["alloc"] + extra_amount(state, i),
            "max_alloc": max_allocation(state, i),
            "soft": soft_boost(state, i),
            "shift": m["shift"],   # optimizer extra that re-optimizing could take back
            "tuition_due": due,
            "tuition_amount": sum(p["amount"] for p in due),
        })

    quarters = []
    for m in months:
        name = quarter_of(m["key"])
        if not quarters or quarters[-1]["label"] != name:
            dates = UCSC_QUARTER_DATES.get(name)
            quarters.append({
                "label": name, "months": [],
                "classes": f"{date_label(dates[0])[:-6]} to {date_label(dates[1])[:-6]}"
                           if dates else None,
                "tuition": None})
        quarters[-1]["months"].append(m["index"])
    by_label = {q["label"]: q for q in quarters}
    for p in tuition:
        by_label[p["quarter"]]["tuition"] = dict(p, max=p["amount"] + takeable)
    for q in quarters:
        q["spending"] = sum(months[i]["alloc"] for i in q["months"])
        q["cat_total"] = sum(months[i]["cat_total"] for i in q["months"])
        q["items"] = [dict(it, month_label=months[i]["label"])
                      for i in q["months"] for it in months[i]["items"]]
        q["items_total"] = sum(it["amount"] for it in q["items"])
        q["month_keys"] = [months[i]["key"] for i in q["months"]]
        q["tuition_amount"] = q["tuition"]["amount"] if q["tuition"] else 0
        q["total"] = q["spending"] + q["tuition_amount"]
        q["over"] = any(months[i]["over"] for i in q["months"])
        # One pool for the quarter: what's assigned to its months minus everything
        # planned in them. A new expense anywhere in the quarter lowers it.
        q["unallocated"] = sum(months[i]["remainder"] for i in q["months"])

    month_sum = sum(m["alloc"] for m in state["months"])
    t_total = tuition_total(state)
    planned = sum(m["used"] for m in months)
    extra = sum(m["extra"] for m in months)
    amounts = {p["amount"] for p in state["tuition"]}
    has_t = bool(state["tuition"])
    return {
        "total": state["total"],               # the term total you entered
        "extra_total": extra,                  # "adds to total" categories, on top
        "available": state["total"] + extra,
        "extra_names": [c["name"] for c in state["categories"] if c["extra"]],
        "start": state["months"][0]["key"],
        "end": state["months"][-1]["key"],
        "first_quarter": quarters[0]["label"],
        "last_quarter": quarters[-1]["label"],
        "quarter_options": quarter_options(),
        "min_total": state["total"] - takeable,
        "tuition_total": t_total,
        "spending_pool": state["total"] - t_total,
        "planned": planned,
        "left": state["total"] + extra - t_total - planned,  # not yet assigned to anything
        "month_sum": month_sum,
        "matches": month_sum + t_total == state["total"],
        "tuition": tuition,
        "tuition_each": amounts.pop() if len(amounts) == 1 else None,
        "max_tuition_each": (t_total + takeable) // len(state["tuition"]) if has_t else None,
        "categories": [dict(c) for c in state["categories"]],
        "optimize": _optimize_view(state, months, quarters),
        "paid": _paid_view(state),
        "months": months,
        "quarters": quarters,
        "unlocked_count": sum(1 for m in state["months"] if not m["locked"]),
    }


def _optimize_view(state, months, quarters):
    plan = optimize_plan(state)
    dyn = dynamic_categories(state)
    total_w = sum(c["weight"] for c in dyn)
    rows = []
    for row, m in zip(plan, months):
        new = sum(row["shares"].values())
        move = row["alloc_new"] - row["alloc"]
        rows.append({"index": row["index"], "label": m["label"], "free": row["free"],
                     "shares": row["shares"], "current": row["current"],
                     "move": move,                         # allocation shifted in from (or out to) the quarter
                     "after": row["free"] + move - new,    # negative: still over by this much
                     "change": new - sum(row["current"].values())})
    q_rows = []
    for q in quarters:
        mine = [rows[i] for i in q["months"]]
        q_rows.append({"label": q["label"], "months": q["months"],
                       "free": sum(max(r["free"], 0) for r in mine),
                       "over": sum(-r["after"] for r in mine if r["after"] < 0),
                       "shares": {c["id"]: sum(r["shares"][c["id"]] for r in mine) for c in dyn},
                       "current": {c["id"]: sum(r["current"][c["id"]] for r in mine) for c in dyn},
                       "after": sum(max(r["after"], 0) for r in mine)})
    give = sum(r["change"] for r in rows if r["change"] > 0)
    take = sum(-r["change"] for r in rows if r["change"] < 0)
    return {
        "static": [c["id"] for c in state["categories"] if c["static"]],
        "dynamic": [{"id": c["id"], "weight": c["weight"],
                     "share": c["weight"] / total_w if total_w else 0} for c in dyn],
        "months": rows,
        "quarters": q_rows,
        "free": sum(max(r["free"], 0) for r in rows),
        "current": sum(sum(r["current"].values()) for r in rows),
        "moved": sum(sum(r["shares"].values()) for r in rows),
        "give": give, "take": take,
        "pending": any(r["change"] or r["move"] or r["shares"] != r["current"] for r in rows),
        "moves": sum(r["move"] for r in rows if r["move"] > 0),
        "still_over": [{"label": r["label"], "by": -r["after"]} for r in rows if r["after"] < 0],
    }


def migrate(data):
    """Bring an older save file up to the current version."""
    if not isinstance(data, dict):
        return data
    if data.get("version") == 1:
        data = dict(data, version=2, tuition=[])
    if data.get("version") == 2:
        try:
            keys = [m["key"] for m in data["months"]]
            old = {p["label"]: p for p in data["tuition"]}
            tuition = []
            for q in tuition_quarters(keys):
                p = old.pop(q, None)
                pay = _payment(q, p["amount"] if p else 0)
                if p:
                    pay["due"] = p["due"]
                tuition.append(pay)
            if not old:  # anything left over can't be placed; let validate reject it
                data = dict(data, version=3, tuition=tuition)
        except (KeyError, TypeError, AttributeError, BudgetError):
            pass
    if data.get("version") == 3:
        data = dict(data, version=4, items=[], next_item_id=1)
    if data.get("version") == 4:
        try:
            cats = [dict(c, static=False, weight=DEFAULT_WEIGHT) for c in data["categories"]]
            data = dict(data, version=5, categories=cats)
        except (KeyError, TypeError):
            pass
    if data.get("version") == 5:
        data = dict(data, version=6, people=[], next_person_id=1, receipts=[], next_receipt_id=1)
    if data.get("version") == 6:
        try:
            cats = [dict(c, extra=False) for c in data["categories"]]
            data = dict(data, version=7, categories=cats)
        except (KeyError, TypeError):
            pass
    if data.get("version") == 7:
        try:
            data = dict(data, version=8, months=[dict(m, boost={}) for m in data["months"]])
        except (KeyError, TypeError):
            pass
    if data.get("version") == 8:
        try:
            data = dict(data, version=9, months=[dict(m, shift=0) for m in data["months"]])
        except (KeyError, TypeError):
            pass
    return data


def validate(data):
    """Check a loaded state. Raises BudgetError if it's damaged."""
    try:
        if data.get("version") != SCHEMA_VERSION:
            raise BudgetError("Unknown save file version.")
        total = data["total"]
        months = data["months"]
        cats = data["categories"]
        ids = {c["id"] for c in cats}
        keys = [m["key"] for m in months]
        ok = (isinstance(total, int) and total > 0 and months
              and isinstance(data["next_cat_id"], int)
              and all(isinstance(c["static"], bool) and isinstance(c["extra"], bool)
                      and isinstance(c["weight"], int)
                      and 0 <= c["weight"] <= 100 for c in cats)
              and all(isinstance(c["name"], str) and isinstance(c["default"], int)
                      and c["default"] >= 0 for c in cats)
              and len(ids) == len(cats))
        for m in months:
            _parse_month(m["key"])
            ok = ok and isinstance(m["alloc"], int) and m["alloc"] >= 0 \
                and isinstance(m["locked"], bool) \
                and all(k in ids and isinstance(v, int) and v >= 0
                        for k, v in m["overrides"].items()) \
                and all(k in ids and isinstance(v, int) and v > 0
                        for k, v in m["boost"].items()) \
                and isinstance(m["shift"], int) and not isinstance(m["shift"], bool)
        for p in data["tuition"]:
            if p["due"] is not None:
                _parse_date(p["due"])
            ok = ok and p["id"] == p["label"] == p["quarter"] \
                and isinstance(p["amount"], int) and p["amount"] >= 0
        ok = ok and [p["quarter"] for p in data["tuition"]] == tuition_quarters(keys)
        ok = ok and isinstance(data["next_item_id"], int) \
            and len({it["id"] for it in data["items"]}) == len(data["items"]) \
            and all(isinstance(it["name"], str) and it["month"] in keys
                    and isinstance(it["amount"], int) and it["amount"] >= 0
                    for it in data["items"])
        people = {p["id"] for p in data["people"]}
        ok = ok and isinstance(data["next_person_id"], int) \
            and isinstance(data["next_receipt_id"], int) \
            and len(people) == len(data["people"]) \
            and all(isinstance(p["name"], str) for p in data["people"])
        for r in data["receipts"]:
            for key in ("date", "from", "to", "reimbursed_date"):
                if r[key] is not None:
                    _parse_date(r[key])
            ok = ok and isinstance(r["amount"], int) and r["amount"] > 0 \
                and isinstance(r["received"], bool) and isinstance(r["reimbursed"], bool) \
                and r["from"] is not None and r["to"] is not None and r["from"] <= r["to"] \
                and (r["payer"] is None or r["payer"] in people)
        ok = ok and len({r["id"] for r in data["receipts"]}) == len(data["receipts"])
        if not ok:
            raise BudgetError("Save file has invalid values.")
        if tuition_total(data) + sum(m["alloc"] for m in months) != total:
            raise BudgetError("Save file doesn't add up to the term total.")
    except (KeyError, TypeError, AttributeError) as e:
        raise BudgetError(f"Save file is missing data ({e}).") from None
    return data
