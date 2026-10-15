"""Turn one request from the page into a change or an answer.

Shared by the local app (budget.py) and the web sync on the server
(server/sync.py), so both apply edits with exactly the same code.
"""

import budget_core as core


def _cents(action, key):
    return core.parse_money(action.get(key))


def apply_action(state, action):
    """Return the new state (None means no budget) for one UI action."""
    kind = action.get("type")
    if kind == "setup":
        if state is not None:
            raise core.BudgetError("A budget already exists. Reset it first.")
        return core.new_budget(_cents(action, "total"), action.get("first_quarter"),
                               action.get("last_quarter"), action.get("start"),
                               _cents(action, "tuition"))
    if kind == "reset":
        return None
    if state is None:
        raise core.BudgetError("Set up a budget first.")

    i = action.get("index")
    if kind == "set_alloc":
        return core.set_month_allocation(state, i, _cents(action, "amount"))
    if kind == "set_lock":
        return core.set_locked(state, i, action.get("locked"))
    if kind == "set_override":
        amount = action.get("amount")
        cents = None if amount in (None, "") else core.parse_money(amount)
        return core.set_override(state, i, action.get("id"), cents)
    if kind == "add_category":
        return core.add_category(state, action.get("name"), _cents(action, "default"))
    if kind == "rename_category":
        return core.rename_category(state, action.get("id"), action.get("name"))
    if kind == "set_category_default":
        return core.set_category_default(state, action.get("id"), _cents(action, "default"))
    if kind == "add_person":
        return core.add_person(state, action.get("name"))
    if kind == "rename_person":
        return core.rename_person(state, action.get("id"), action.get("name"))
    if kind == "delete_person":
        return core.delete_person(state, action.get("id"))
    if kind in ("add_receipt", "update_receipt"):
        fields = {k: action[k] for k in core._RECEIPT_FIELDS if k in action}
        if "amount" in fields:
            fields["amount"] = core.parse_money(fields["amount"])
        if kind == "add_receipt":
            return core.add_receipt(state, **fields)
        return core.update_receipt(state, action.get("id"), **fields)
    if kind == "delete_receipt":
        return core.delete_receipt(state, action.get("id"))
    if kind == "set_category_extra":
        return core.set_category_extra(state, action.get("id"), action.get("extra"))
    if kind == "set_category_static":
        return core.set_category_static(state, action.get("id"), action.get("static"))
    if kind == "set_category_weight":
        return core.set_category_weight(state, action.get("id"), action.get("weight"))
    if kind == "optimize":
        return core.apply_optimize(state)
    if kind == "unoptimize":
        return core.clear_optimize(state)
    if kind == "delete_category":
        return core.delete_category(state, action.get("id"))
    if kind == "add_item":
        return core.add_item(state, action.get("name"), action.get("month"),
                             _cents(action, "amount"))
    if kind == "update_item":
        amount = action.get("amount")
        return core.update_item(state, action.get("id"), action.get("name"), action.get("month"),
                                None if amount is None else core.parse_money(amount))
    if kind == "delete_item":
        return core.delete_item(state, action.get("id"))
    if kind == "set_tuition":
        return core.set_tuition(state, _cents(action, "amount"), action.get("id"))
    if kind == "set_total":
        return core.set_total(state, _cents(action, "amount"))
    if kind == "set_term_quarters":
        return core.set_term_quarters(state, action.get("first_quarter"),
                                      action.get("last_quarter"), action.get("start"))
    if kind == "set_tuition_due":
        return core.set_tuition_due(state, action.get("id"), action.get("due"))
    raise core.BudgetError(f"Unknown action: {kind!r}")


def answer_query(state, q):
    """Read-only lookups for live previews. Never changes or saves anything."""
    if state is None:
        raise core.BudgetError("Set up a budget first.")
    kind = q.get("type")
    if kind == "coverage":
        amount = q.get("amount")
        cents = core.parse_money(amount) if amount not in (None, "") else 0
        return core.coverage(state, cents, q.get("from"), q.get("to"))
    if kind == "request":
        exclude = q.get("exclude") or []
        if not isinstance(exclude, list):
            raise core.BudgetError("Bad list of parts to leave out.")
        return core.request_quote(state, q.get("as_of"), q.get("through"),
                                  bool(q.get("include_expected")), exclude)
    if kind == "starts":
        return core.period_starts(state, q.get("today"))
    if kind == "periods":
        return core.period_ends(state, q.get("as_of"))
    if kind == "quarter_left":
        return core.quarter_left(state, q.get("quarter"), q.get("date"))
    raise core.BudgetError(f"Unknown question: {kind!r}")
