"""Tests for the split and redistribution logic. Run: python -m unittest -v"""

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

import budget_core as core
from budget import Store, apply_action


def budget(total, n=11, start="2026-10"):
    """A budget with $0 tuition, for testing the month logic on its own."""
    keys = core.month_range(start, "2030-12")[:n]
    return core.new_budget_from_months(total, keys[0], keys[-1])


def ucsc(total=3_000_000, each=500_000):
    """$30,000 term, Fall 2026 to Summer 2027 (Sep to Aug), three UCSC tuition payments."""
    return core.new_budget(total, "Fall 2026", "Summer 2027", None, each)


def allocs(state):
    return [m["alloc"] for m in state["months"]]


def assert_balanced(tc, state):
    tc.assertEqual(core.tuition_total(state) + sum(allocs(state)), state["total"])


class EqualSplit(unittest.TestCase):
    def test_default_term_is_eleven_months(self):
        s = core.new_budget_from_months(1_100_000, "2026-10", "2027-08")
        self.assertEqual(s["months"][0]["key"], "2026-10")
        self.assertEqual(s["months"][-1]["key"], "2027-08")
        self.assertEqual(len(s["months"]), 11)
        self.assertEqual(set(allocs(s)), {100_000})

    def test_uneven_cents_go_to_earliest_months(self):
        s = budget(1_000_005)  # $10,000.05 over 11 months
        a = allocs(s)
        base = 1_000_005 // 11  # 90909, remainder 6
        self.assertEqual(a, [base + 1] * 6 + [base] * 5)
        assert_balanced(self, s)

    def test_single_cent_and_tiny_totals(self):
        self.assertEqual(allocs(budget(1, n=3)), [1, 0, 0])
        self.assertEqual(allocs(budget(10, n=3)), [4, 3, 3])

    def test_split_even_negative(self):
        parts = core.split_even(-10, 3)
        self.assertEqual(parts, [-4, -3, -3])
        self.assertEqual(sum(parts), -10)

    def test_rejects_bad_setup(self):
        with self.assertRaises(core.BudgetError):
            core.new_budget_from_months(0, "2026-10", "2027-08")
        with self.assertRaises(core.BudgetError):
            core.new_budget_from_months(100, "2027-08", "2026-10")
        with self.assertRaises(core.BudgetError):
            core.new_budget_from_months(100, "2026-13", "2027-08")


class Nudging(unittest.TestCase):
    def test_raise_takes_evenly_from_others(self):
        s = budget(1_100_000)
        s2 = core.set_month_allocation(s, 0, 110_000)  # +$100 from 10 months
        self.assertEqual(allocs(s2), [110_000] + [99_000] * 10)
        assert_balanced(self, s2)

    def test_lower_gives_evenly_to_others(self):
        s = budget(1_100_000)
        s2 = core.set_month_allocation(s, 5, 90_000)
        self.assertEqual(s2["months"][5]["alloc"], 90_000)
        self.assertTrue(all(a == 101_000 for i, a in enumerate(allocs(s2)) if i != 5))

    def test_locked_months_never_change(self):
        s = budget(1_100_000)
        s = core.set_locked(s, 1, True)
        s = core.set_locked(s, 2, True)
        s2 = core.set_month_allocation(s, 0, 108_000)  # +$80 over 8 unlocked
        self.assertEqual(s2["months"][1]["alloc"], 100_000)
        self.assertEqual(s2["months"][2]["alloc"], 100_000)
        self.assertEqual(allocs(s2)[3:], [99_000] * 8)
        assert_balanced(self, s2)

    def test_leftover_pennies_hit_earliest_unlocked(self):
        s = budget(1_100_000)
        s = core.set_locked(s, 1, True)
        # Lower month 0 by 7 cents across 9 unlocked months (2..10).
        s2 = core.set_month_allocation(s, 0, 100_000 - 7)
        a = allocs(s2)
        self.assertEqual(a[1], 100_000)            # locked, untouched
        self.assertEqual(a[2:9], [100_001] * 7)    # earliest unlocked get the pennies
        self.assertEqual(a[9:], [100_000] * 2)
        assert_balanced(self, s2)

    def test_editing_a_locked_month_directly_is_allowed(self):
        s = core.set_locked(budget(1_100_000), 0, True)
        s2 = core.set_month_allocation(s, 0, 50_000)
        self.assertEqual(s2["months"][0]["alloc"], 50_000)
        assert_balanced(self, s2)

    def test_input_state_is_not_mutated(self):
        s = budget(1_100_000)
        before = copy.deepcopy(s)
        core.set_month_allocation(s, 0, 150_000)
        self.assertEqual(s, before)


class Blocked(unittest.TestCase):
    def test_would_push_other_month_below_zero(self):
        s = budget(300, n=3)  # [100, 100, 100]
        with self.assertRaises(core.BudgetError) as e:
            core.set_month_allocation(s, 0, 301)
        self.assertIn("spending money in the term", str(e.exception))
        s = core.set_locked(s, 2, True)
        with self.assertRaises(core.BudgetError) as e:
            core.set_month_allocation(s, 0, 201)  # month 1 would hit -1
        self.assertIn("below $0.00", str(e.exception))
        self.assertIn("$2.00", str(e.exception))  # the max allowed

    def test_would_push_other_month_below_category_total(self):
        s = budget(300_000, n=3)  # $1,000 each
        s = core.add_category(s, "Rent", 90_000)  # $900 every month
        with self.assertRaises(core.BudgetError) as e:
            core.set_month_allocation(s, 0, 130_000)  # others would drop to $850
        msg = str(e.exception)
        self.assertIn("below what's planned in it ($900.00)", msg)
        self.assertIn("The most Oct 2026 can be right now is $1,200.00", msg)
        self.assertEqual(core.max_allocation(s, 0), 120_000)
        core.set_month_allocation(s, 0, 120_000)  # exactly the max works

    def test_override_raises_one_months_floor(self):
        s = budget(300_000, n=3)
        s = core.add_category(s, "Food", 10_000)
        s = core.set_override(s, 2, "c1", 100_000)
        with self.assertRaises(core.BudgetError):
            core.set_month_allocation(s, 0, 100_002)  # takes a cent from month 2

    def test_all_others_locked(self):
        s = budget(300, n=3)
        s = core.set_locked(core.set_locked(s, 1, True), 2, True)
        with self.assertRaises(core.BudgetError) as e:
            core.set_month_allocation(s, 0, 50)
        self.assertIn("locked", str(e.exception))

    def test_negative_blocked(self):
        with self.assertRaises(core.BudgetError):
            core.set_month_allocation(budget(300, n=3), 0, -1)

    def test_month_already_over_can_still_receive(self):
        s = budget(300_000, n=3)
        s = core.add_category(s, "Rent", 150_000)  # every month over
        s2 = core.set_month_allocation(s, 0, 50_000)  # gives money away
        self.assertEqual(allocs(s2), [50_000, 125_000, 125_000])


class TotalAlwaysMatches(unittest.TestCase):
    def test_random_operations_keep_total(self):
        rng = random.Random(42)
        s = ucsc(5_234_567, 900_001)
        s = core.add_category(s, "Food", 30_000)
        s = core.add_category(s, "Gas", 8_000)
        for _ in range(2000):
            op = rng.random()
            i = rng.randrange(12)
            try:
                if op < 0.05:
                    pid = rng.choice([None, "Fall 2026", "Winter 2027", "Spring 2027"])
                    s = core.set_tuition(s, rng.randrange(0, 1_500_000), pid)
                elif op < 0.1:
                    s = core.set_total(s, rng.randrange(1, 9_000_000))
                elif op < 0.6:
                    s = core.set_month_allocation(s, i, rng.randrange(0, 600_000))
                elif op < 0.75:
                    s = core.set_locked(s, i, rng.random() < 0.3)
                elif op < 0.9:
                    s = core.set_override(s, i, rng.choice(["c1", "c2"]), rng.randrange(0, 60_000))
                elif op < 0.95:
                    s = core.add_item(s, "x", s["months"][i]["key"], rng.randrange(0, 40_000))
                else:
                    s = core.set_override(s, i, "c1", None)
            except core.BudgetError:
                pass
            assert_balanced(self, s)
            self.assertTrue(all(a >= 0 for a in allocs(s)))
            core.validate(s)


class Tuition(unittest.TestCase):
    def test_setup_reserves_tuition_then_splits(self):
        s = ucsc()
        self.assertEqual([p["due"] for p in s["tuition"]],
                         ["2026-09-17", "2026-12-16", "2027-03-23"])
        self.assertEqual(len(s["months"]), 12)
        self.assertEqual(core.spending_pool(s), 1_500_000)
        self.assertEqual(set(allocs(s)), {125_000})
        assert_balanced(self, s)

    def test_plan_can_start_mid_quarter_and_keep_its_tuition(self):
        s = core.new_budget(3_000_000, "Fall 2026", "Summer 2027", "2026-10", 500_000)
        self.assertEqual(s["months"][0]["key"], "2026-10")
        self.assertEqual(len(s["months"]), 11)
        self.assertEqual([p["quarter"] for p in s["tuition"]], ["Fall 2026", "Winter 2027", "Spring 2027"])
        v = core.summarize(s)
        self.assertTrue(v["tuition"][0]["before_start"])  # Fall due Sep 17, plan starts Oct
        assert_balanced(self, s)

    def test_start_month_must_be_in_first_quarter(self):
        with self.assertRaises(core.BudgetError) as e:
            core.new_budget(3_000_000, "Fall 2026", "Summer 2027", "2027-01", 0)
        self.assertIn("Sep 2026 to Dec 2026", str(e.exception))
        with self.assertRaises(core.BudgetError):
            core.new_budget(3_000_000, "Summer 2027", "Fall 2026")
        with self.assertRaises(core.BudgetError):
            core.new_budget(3_000_000, "Autumn 2026", "Summer 2027")

    def test_tuition_more_than_total_is_rejected(self):
        with self.assertRaises(core.BudgetError):
            core.new_budget(1_000_000, "Fall 2026", "Summer 2027", None, 400_000)

    def test_raising_all_takes_evenly_from_unlocked_months(self):
        s = core.set_locked(ucsc(), 0, True)
        s2 = core.set_tuition(s, 510_000)  # +$300 total over 11 unlocked months
        self.assertEqual({p["amount"] for p in s2["tuition"]}, {510_000})
        a = allocs(s2)
        self.assertEqual(a[0], 125_000)  # locked
        self.assertEqual(a[1:], [125_000 - 2728] * 3 + [125_000 - 2727] * 8)
        assert_balanced(self, s2)

    def test_one_payment_and_lowering_gives_back(self):
        s = core.set_tuition(ucsc(), 440_000, "Spring 2027")  # $600 back to 12 months
        self.assertEqual([p["amount"] for p in s["tuition"]], [500_000, 500_000, 440_000])
        self.assertEqual(set(allocs(s)), {130_000})
        assert_balanced(self, s)

    def test_blocked_by_category_floor_with_max(self):
        s = core.add_category(ucsc(), "Rent", 120_000)  # $1,200 of $1,250 each month
        with self.assertRaises(core.BudgetError) as e:
            core.set_tuition(s, 600_000)  # would take $250 from each month
        msg = str(e.exception)
        self.assertIn("below what's planned in it ($1,200.00)", msg)
        self.assertIn("The most it can be right now is $5,200.00 per quarter", msg)
        self.assertEqual(core.max_tuition_each(s), 520_000)
        core.set_tuition(s, 520_000)

    def test_all_months_locked_blocks_tuition_change(self):
        s = ucsc()
        for i in range(12):
            s = core.set_locked(s, i, True)
        with self.assertRaises(core.BudgetError):
            core.set_tuition(s, 400_000)

    def test_month_cant_exceed_spending_pool(self):
        with self.assertRaises(core.BudgetError) as e:
            core.set_month_allocation(ucsc(), 0, 1_500_001)
        self.assertIn("after tuition", str(e.exception))

    def test_due_date_can_be_anywhere(self):
        s = core.set_tuition_due(ucsc(), "Winter 2027", "2027-01-05")
        self.assertEqual(core.summarize(s)["months"][4]["tuition_amount"], 500_000)
        s = core.set_tuition_due(s, "Fall 2026", "2026-08-30")  # before the plan: allowed
        self.assertTrue(core.summarize(s)["tuition"][0]["before_start"])
        s = core.set_tuition_due(s, "Fall 2026", "")             # cleared
        self.assertIsNone(s["tuition"][0]["due"])
        core.validate(s)
        with self.assertRaises(core.BudgetError):
            core.set_tuition_due(s, "Fall 2026", "not a date")

    def test_summary_places_payments_in_months(self):
        v = core.summarize(ucsc())
        due = {m["key"]: m["tuition_amount"] for m in v["months"] if m["tuition_due"]}
        self.assertEqual(due, {"2026-09": 500_000, "2026-12": 500_000, "2027-03": 500_000})
        self.assertTrue(v["matches"])
        self.assertEqual(v["tuition_each"], 500_000)

    def test_old_save_files_migrate(self):
        v1 = {"version": 1, "total": 1_100_000, "categories": [], "next_cat_id": 1,
              "months": [{"key": k, "alloc": 100_000, "locked": False, "overrides": {}}
                         for k in core.month_range("2026-10", "2027-08")]}
        new = core.validate(core.migrate(v1))
        self.assertEqual(new["version"], core.SCHEMA_VERSION)
        self.assertEqual([(p["quarter"], p["amount"]) for p in new["tuition"]],
                         [("Fall 2026", 0), ("Winter 2027", 0), ("Spring 2027", 0)])
        v2 = dict(v1, version=2, total=1_200_000, tuition=[
            {"id": "winter", "label": "Winter 2027", "due": "2027-01-02", "amount": 100_000}])
        new = core.validate(core.migrate(v2))
        self.assertEqual([(p["id"], p["due"], p["amount"]) for p in new["tuition"]],
                         [("Fall 2026", "2026-09-17", 0), ("Winter 2027", "2027-01-02", 100_000),
                          ("Spring 2027", "2027-03-23", 0)])

class TermEdits(unittest.TestCase):
    def test_raise_total_goes_to_unlocked_months(self):
        s = core.set_locked(ucsc(), 0, True)
        s2 = core.set_total(s, 3_011_000)  # +$110 over 11 unlocked months
        self.assertEqual(allocs(s2), [125_000] + [126_000] * 11)
        self.assertEqual(core.tuition_total(s2), 1_500_000)  # tuition untouched
        assert_balanced(self, s2)

    def test_lower_total_blocked_with_minimum(self):
        s = core.add_category(ucsc(), "Rent", 120_000)
        self.assertEqual(core.min_total(s), 3_000_000 - 12 * 5_000)
        with self.assertRaises(core.BudgetError) as e:
            core.set_total(s, 2_900_000)
        self.assertIn("The lowest it can go right now is $29,400.00", str(e.exception))
        assert_balanced(self, core.set_total(s, 2_940_000))

    def test_quarters_keep_data_and_resplit(self):
        s = core.add_category(budget(1_100_000), "Food", 10_000)  # Oct-Aug, $0 tuition
        s = core.set_override(s, 2, "c1", 20_000)   # Dec override
        s = core.set_locked(core.set_month_allocation(s, 1, 50_000), 1, True)  # Nov locked
        s2 = core.set_term_quarters(s, "Fall 2026", "Summer 2027", "2026-09")
        self.assertEqual(len(s2["months"]), 12)
        self.assertEqual(s2["months"][0]["key"], "2026-09")
        nov = s2["months"][2]
        self.assertEqual((nov["alloc"], nov["locked"]), (50_000, True))
        self.assertEqual(s2["months"][3]["overrides"], {"c1": 20_000})
        self.assertEqual(sorted(set(a for i, a in enumerate(allocs(s2)) if i != 2)), [95_454, 95_455])
        assert_balanced(self, s2)

    def test_dropping_september_keeps_fall_tuition(self):
        s = core.set_term_quarters(ucsc(), "Fall 2026", "Summer 2027", "2026-10")
        self.assertEqual(len(s["months"]), 11)
        self.assertEqual(core.tuition_total(s), 1_500_000)
        self.assertEqual(sorted(set(allocs(s))), [136_363, 136_364])
        assert_balanced(self, s)

    def test_added_quarter_gets_same_tuition_removed_quarter_gives_it_back(self):
        s = core.set_term_quarters(ucsc(), "Fall 2026", "Fall 2027")
        self.assertEqual([(p["quarter"], p["amount"], p["due"]) for p in s["tuition"]][-1],
                         ("Fall 2027", 500_000, None))
        self.assertEqual(core.tuition_total(s), 2_000_000)
        assert_balanced(self, s)
        s = core.set_term_quarters(ucsc(), "Fall 2026", "Winter 2027")
        self.assertEqual(core.tuition_total(s), 1_000_000)
        self.assertEqual(core.spending_pool(s), 2_000_000)
        assert_balanced(self, s)

    def test_term_change_blocked_when_tuition_exceeds_total(self):
        with self.assertRaises(core.BudgetError) as e:
            core.set_term_quarters(ucsc(3_000_000, 900_000), "Fall 2026", "Fall 2027")
        self.assertIn("more than the term total", str(e.exception))

class Quarters(unittest.TestCase):
    def test_tree_groups_months_and_tuition(self):
        v = core.summarize(ucsc())
        q = {x["label"]: x for x in v["quarters"]}
        self.assertEqual(list(q), ["Fall 2026", "Winter 2027", "Spring 2027", "Summer 2027"])
        self.assertEqual([len(x["months"]) for x in v["quarters"]], [4, 3, 3, 2])
        self.assertEqual(q["Winter 2027"]["tuition"]["due"], "2026-12-16")  # paid in Dec, belongs to Winter
        self.assertIsNone(q["Summer 2027"]["tuition"])
        self.assertEqual(q["Fall 2026"]["total"], 4 * 125_000 + 500_000)
        self.assertEqual(q["Fall 2026"]["classes"], "Sep 19 to Dec 11")
        self.assertEqual(sum(x["total"] for x in v["quarters"]), v["total"])


class Categories(unittest.TestCase):
    def test_defaults_overrides_and_delete(self):
        s = budget(1_100_000)
        s = core.add_category(s, "Food", 40_000)
        s = core.add_category(s, "Gas", 10_000)
        s = core.set_override(s, 3, "c1", 55_000)
        v = core.summarize(s)
        self.assertEqual(v["months"][0]["cat_total"], 50_000)
        self.assertEqual(v["months"][3]["cat_total"], 65_000)
        self.assertEqual(v["months"][3]["remainder"], 35_000)
        s = core.rename_category(s, "c1", "Groceries")
        self.assertEqual(core.summarize(s)["months"][7]["categories"][0]["name"], "Groceries")
        s = core.delete_category(s, "c1")
        self.assertEqual(s["months"][3]["overrides"], {})
        self.assertEqual(core.summarize(s)["months"][3]["cat_total"], 10_000)

    def test_over_flag(self):
        s = core.add_category(budget(300, n=3), "Rent", 101)
        self.assertTrue(all(m["over"] for m in core.summarize(s)["months"]))

    def test_duplicate_and_blank_names(self):
        s = core.add_category(budget(300, n=3), "Food", 0)
        for bad in ["food", "  ", ""]:
            with self.assertRaises(core.BudgetError):
                core.add_category(s, bad, 0)


class Money(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(core.parse_money("1,250.5"), 125_050)
        self.assertEqual(core.parse_money("$80"), 8_000)
        self.assertEqual(core.parse_money(".07"), 7)
        for bad in ["-5", "1.234", "abc", "", "1e3"]:
            with self.assertRaises(core.BudgetError):
                core.parse_money(bad)

    def test_format(self):
        self.assertEqual(core.format_money(123_456_789), "$1,234,567.89")
        self.assertEqual(core.format_money(-5), "-$0.05")


class Storage(unittest.TestCase):
    def test_save_load_backup_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            self.assertIsNone(store.load())
            s1 = apply_action(None, {"type": "setup", "total": "1100", "tuition": "100", "first_quarter": "Fall 2026", "last_quarter": "Summer 2027"})
            store.save(s1)
            s2 = apply_action(s1, {"type": "set_alloc", "index": 0, "amount": "150"})
            store.save(s2)
            self.assertEqual(store.load(), s2)
            self.assertEqual(json.loads(Path(d, "budget.backup.json").read_text()), s1)
            self.assertFalse(list(Path(d).glob("*.tmp")))
            store.clear()
            self.assertIsNone(store.load())
            self.assertEqual(json.loads(Path(d, "budget.backup.json").read_text()), s2)

    def test_corrupt_main_falls_back_to_backup(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            s = budget(1000, n=2)
            store.save(s)
            store.save(s)  # now backup exists too
            Path(d, "budget.json").write_text("{not json")
            self.assertEqual(store.load(), s)



class Left(unittest.TestCase):
    def test_total_left_after_tuition_and_categories(self):
        s = core.add_category(ucsc(), "Rent", 100_000)  # $1,000 x 12 months
        s = core.set_override(s, 0, "c1", 50_000)       # Sep only $500
        v = core.summarize(s)
        self.assertEqual(v["planned"], 11 * 100_000 + 50_000)
        self.assertEqual(v["left"], 3_000_000 - 1_500_000 - 1_150_000)
        s = core.set_tuition(s, 400_000, "Winter 2027")  # one quarter cheaper
        self.assertEqual(core.summarize(s)["left"], 3_000_000 - 1_400_000 - 1_150_000)


class IndividualExpenses(unittest.TestCase):
    def test_add_edit_delete_without_moving_money(self):
        s = ucsc()
        s2 = core.add_item(s, "  Tournament   travel ", "2027-02", 22_000)
        self.assertEqual(allocs(s2), allocs(s))  # nothing moves between months
        it = s2["items"][0]
        self.assertEqual((it["id"], it["name"], it["month"]), ("i1", "Tournament travel", "2027-02"))
        feb = core.summarize(s2)["months"][5]
        self.assertEqual((feb["items_total"], feb["used"], feb["remainder"]), (22_000, 22_000, 125_000 - 22_000))
        s2 = core.update_item(s2, "i1", amount=30_000, month="2027-03", name="Regionals")
        self.assertEqual(s2["items"][0], {"id": "i1", "name": "Regionals", "month": "2027-03", "amount": 30_000})
        self.assertEqual(core.delete_item(s2, "i1")["items"], [])
        assert_balanced(self, s2)

    def test_month_must_be_in_term_and_name_required(self):
        with self.assertRaises(core.BudgetError):
            core.add_item(ucsc(), "Camp", "2027-09", 100)
        with self.assertRaises(core.BudgetError):
            core.add_item(ucsc(), "  ", "2027-02", 100)

    def test_expense_goes_over_and_counts_as_floor(self):
        s = core.add_item(ucsc(), "Uniform", "2026-10", 130_000)  # more than Oct's $1,250
        v = core.summarize(s)
        self.assertTrue(v["months"][1]["over"])
        s = core.add_item(s, "Fees", "2026-11", 120_000)
        with self.assertRaises(core.BudgetError) as e:  # Nov can't give up $100
            core.set_month_allocation(s, 0, 125_000 + 1_100_00)
        self.assertIn("Nov 2026 would drop to", str(e.exception))

    def test_quarter_groups_expenses_and_total_left_counts_them(self):
        s = core.add_item(ucsc(), "Uniform", "2026-10", 8_000)
        s = core.add_item(s, "Travel", "2027-02", 22_000)
        v = core.summarize(s)
        q = {x["label"]: x for x in v["quarters"]}
        self.assertEqual([(i["name"], i["month_label"]) for i in q["Fall 2026"]["items"]], [("Uniform", "Oct 2026")])
        self.assertEqual(q["Winter 2027"]["items_total"], 22_000)
        self.assertEqual(v["left"], 3_000_000 - 1_500_000 - 30_000)

    def test_term_change_wont_strand_expenses(self):
        s = core.add_item(ucsc(), "Camp", "2026-09", 5_000)
        with self.assertRaises(core.BudgetError) as e:
            core.set_term_quarters(s, "Fall 2026", "Summer 2027", "2026-10")
        self.assertIn("Camp (Sep 2026)", str(e.exception))

    def test_v3_files_get_an_empty_expense_list(self):
        s = ucsc()
        old = {k: v for k, v in s.items() if k not in ("items", "next_item_id")}
        old["version"] = 3
        new = core.validate(core.migrate(old))
        self.assertEqual((new["items"], new["next_item_id"], new["version"]), ([], 1, core.SCHEMA_VERSION))


class Optimizer(unittest.TestCase):
    def setUp(self):
        s = ucsc()                                   # $1,250 per month
        s = core.add_category(s, "Rent", 90_000)     # c1
        s = core.add_category(s, "Food", 20_000)     # c2
        s = core.add_category(s, "Projects", 5_000)  # c3
        s = core.set_category_static(s, "c1", True)
        s = core.set_category_weight(s, "c2", 50)
        s = core.set_category_weight(s, "c3", 30)
        self.s = s                                   # $100 unallocated each month

    def test_weighted_split_whole_cents(self):
        self.assertEqual(core.weighted_split(10_000, [50, 30]), [6_250, 3_750])
        self.assertEqual(core.weighted_split(1, [50, 30]), [1, 0])
        self.assertEqual(sum(core.weighted_split(10_001, [33, 33, 33])), 10_001)
        self.assertEqual(core.weighted_split(500, [0, 0]), [0, 0])
        self.assertEqual(core.weighted_split(-5, [1]), [0])

    def test_static_category_is_locked(self):
        for change in (lambda: core.set_category_default(self.s, "c1", 1),
                       lambda: core.set_override(self.s, 0, "c1", 1),
                       lambda: core.delete_category(self.s, "c1")):
            with self.assertRaises(core.BudgetError) as e:
                change()
            self.assertIn("Rent is static", str(e.exception))
        core.set_category_default(core.set_category_static(self.s, "c1", False), "c1", 1)

    def test_preview_splits_by_slider_and_skips_static(self):
        v = core.summarize(self.s)["optimize"]
        self.assertEqual(v["static"], ["c1"])
        self.assertEqual([(d["id"], d["share"]) for d in v["dynamic"]], [("c2", 0.625), ("c3", 0.375)])
        self.assertEqual(v["months"][0]["shares"], {"c2": 6_250, "c3": 3_750})
        self.assertEqual(v["free"], 12 * 10_000)
        self.assertEqual(v["quarters"][0]["shares"], {"c2": 4 * 6_250, "c3": 4 * 3_750})

    def test_apply_fills_dynamic_categories_and_balances(self):
        s = core.apply_optimize(self.s)
        self.assertEqual(allocs(s), allocs(self.s))  # allocations don't move
        for m in core.summarize(s)["months"]:
            self.assertEqual(m["remainder"], 0)
        self.assertEqual(s["months"][0]["overrides"], {})            # your amounts untouched
        self.assertEqual(s["months"][0]["boost"], {"c2": 6_250, "c3": 3_750})
        food = core.summarize(s)["months"][0]["categories"][1]
        self.assertEqual((food["base"], food["boost"], food["amount"]), (20_000, 6_250, 26_250))
        assert_balanced(self, s)
        with self.assertRaises(core.BudgetError):  # already optimized
            core.apply_optimize(s)

    def test_over_month_is_covered_by_its_quarter(self):
        s = core.add_item(self.s, "Trip", "2026-10", 20_000)  # Oct over by $100
        v = core.summarize(s)["optimize"]
        fall = [r for r in v["months"] if r["label"].endswith("2026")]
        self.assertEqual({tuple(r["shares"].items()) for r in fall}, {(("c2", 3_125), ("c3", 1_875))})
        self.assertEqual(v["months"][1]["move"], 15_000)       # Oct borrows from Sep, Nov, Dec
        s = core.apply_optimize(s)
        self.assertEqual(allocs(s)[:4], [120_000, 140_000, 120_000, 120_000])  # each $1,150 planned + $50
        self.assertTrue(all(m["remainder"] == 0 for m in core.summarize(s)["months"][:4]))
        assert_balanced(self, s)

    def test_nothing_to_optimize_into(self):
        s = core.set_category_weight(core.set_category_weight(self.s, "c2", 0), "c3", 0)
        with self.assertRaises(core.BudgetError):
            core.apply_optimize(s)
        s = core.set_category_static(core.set_category_static(self.s, "c2", True), "c3", True)
        with self.assertRaises(core.BudgetError):
            core.apply_optimize(s)
        with self.assertRaises(core.BudgetError):
            core.set_category_weight(self.s, "c2", 101)

    def test_v4_categories_become_dynamic_at_half(self):
        old = dict(self.s, version=4, categories=[{"id": "c1", "name": "Rent", "default": 1}])
        old["months"] = [dict(m, overrides={}) for m in old["months"]]
        new = core.migrate(old)
        self.assertEqual(new["categories"][0], {"id": "c1", "name": "Rent", "default": 1,
                                                "static": False, "weight": 50, "extra": False})


class MoneyIn(unittest.TestCase):
    """ucsc(): $1,250 per month Sep 2026 to Aug 2027, $5,000 tuition each quarter."""

    def test_need_prorates_by_day_and_adds_tuition_due(self):
        s = ucsc()
        n = core.need_for_range(s, "2026-10-01", "2026-12-31")  # Oct, Nov, Dec in full
        self.assertEqual((n["spending"], n["tuition"]), (3 * 125_000, 500_000))  # Winter due Dec 16
        n = core.need_for_range(s, "2026-11-16", "2026-11-30")  # 15 of Nov's 30 days
        self.assertEqual((n["spending"], n["tuition"], n["outside"]), (62_500, 0, False))
        self.assertTrue(core.need_for_range(s, "2027-08-01", "2027-09-30")["outside"])
        with self.assertRaises(core.BudgetError):
            core.need_for_range(s, "2026-12-01", "2026-11-01")

    def test_coverage_says_enough_or_short(self):
        s = ucsc()
        self.assertEqual(core.coverage(s, 900_000, "2026-10-01", "2026-12-31")["diff"], 25_000)
        self.assertEqual(core.coverage(s, 100_000, "2026-10-01", "2026-10-31")["diff"], -25_000)

    def test_people_and_receipts(self):
        s = core.add_person(ucsc(), " Mom ")
        with self.assertRaises(core.BudgetError):
            core.add_person(s, "mom")
        s = core.add_receipt(s, date="2026-10-01", amount=300_000, payer="p1",
                             **{"from": "2026-10-01", "to": "2026-11-30"})
        r = s["receipts"][0]
        self.assertEqual((r["id"], r["received"], r["reimbursed"]), ("r1", True, False))
        with self.assertRaises(core.BudgetError) as e:
            core.delete_person(s, "p1")
        self.assertIn("Mom is listed on 1 payment", str(e.exception))
        s = core.update_receipt(s, "r1", reimbursed=True, reimbursed_date="2026-11-02")
        v = core.summarize(s)["paid"]
        self.assertEqual((v["received"], v["not_reimbursed"]), (300_000, 0))
        self.assertEqual(v["receipts"][0]["payer_name"], "Mom")
        self.assertEqual(v["receipts"][0]["check"]["diff"], 300_000 - 2 * 125_000)
        s = core.update_receipt(s, "r1", reimbursed=False)
        self.assertIsNone(s["receipts"][0]["reimbursed_date"])
        self.assertEqual(allocs(s), allocs(ucsc()))  # a log only: the plan never moves
        assert_balanced(self, s)
        core.validate(s)
        self.assertEqual(core.delete_receipt(s, "r1")["receipts"], [])

    def test_receipt_checks(self):
        s = ucsc()
        base = {"amount": 100, "from": "2026-10-01", "to": "2026-10-31", "date": "2026-10-01"}
        for bad in ({"amount": 0}, {"to": "2026-09-01"}, {"from": None},
                    {"date": None}, {"payer": "p9"}, {"date": "nope"}):
            with self.assertRaises(core.BudgetError):
                core.add_receipt(s, **dict(base, **bad))
        expected = core.add_receipt(s, **dict(base, received=False, date=None))
        self.assertEqual(core.summarize(expected)["paid"]["expected"], 100)

    def test_quarter_left_as_of_a_date(self):
        s = ucsc()
        left = core.quarter_left(s, "Fall 2026", "2026-11-16")  # half of Nov + all of Dec
        self.assertEqual((left["spending"], left["of"], left["tuition"]), (62_500 + 125_000, 500_000, 0))
        left = core.quarter_left(s, "Winter 2027", "2026-12-01")  # before Winter: all of it, tuition not due yet
        self.assertEqual((left["spending"], left["tuition"]), (375_000, 500_000))
        self.assertTrue(core.quarter_left(s, "Fall 2026", "2027-01-01")["ended"])
        with self.assertRaises(core.BudgetError):
            core.quarter_left(s, "Fall 2030", "2026-11-16")

    def test_v5_files_upgrade(self):
        old = {k: v for k, v in ucsc().items() if k not in ("people", "next_person_id", "receipts", "next_receipt_id")}
        new = core.validate(core.migrate(dict(old, version=5)))
        self.assertEqual((new["people"], new["receipts"], new["version"]), ([], [], core.SCHEMA_VERSION))


class AddsToTotal(unittest.TestCase):
    """Gas paid by money on top of the term total."""

    def setUp(self):
        s = core.add_category(ucsc(), "Rent", 90_000)   # c1, $1,250 per month
        s = core.add_category(s, "Gas", 12_000)         # c2
        s = core.set_override(s, 3, "c2", 20_000)       # Dec gas $200
        self.plain = s
        self.s = core.set_category_extra(s, "c2", True)

    def test_raises_available_and_months_not_the_term_total(self):
        v = core.summarize(self.s)
        self.assertEqual(v["total"], 3_000_000)                     # what you entered
        self.assertEqual(v["extra_total"], 11 * 12_000 + 20_000)
        self.assertEqual(v["available"], 3_000_000 + 11 * 12_000 + 20_000)
        dec = v["months"][3]
        self.assertEqual((dec["alloc_base"], dec["extra"], dec["alloc"]), (125_000, 20_000, 145_000))
        self.assertEqual(dec["remainder"], 125_000 - 90_000)        # gas doesn't eat Rent's month
        self.assertEqual(v["extra_names"], ["Gas"])
        self.assertTrue(v["matches"])
        assert_balanced(self, self.s)

    def test_total_left_unchanged_by_extra_but_reduced_without_it(self):
        with_extra = core.summarize(self.s)["left"]
        without = core.summarize(self.plain)["left"]
        self.assertEqual(with_extra, 3_000_000 - 1_500_000 - 12 * 90_000)
        self.assertEqual(without, with_extra - (11 * 12_000 + 20_000))

    def test_extra_doesnt_block_redistribution_or_get_optimized(self):
        self.assertEqual(core.committed(self.s, 0), 90_000)
        self.assertNotIn("c2", [c["id"] for c in core.dynamic_categories(self.s)])
        s = core.apply_optimize(core.add_category(self.s, "Food", 0))
        self.assertNotIn("c2", s["months"][0]["boost"])

    def test_need_and_quarter_left_include_extra(self):
        n = core.need_for_range(self.s, "2026-12-01", "2026-12-31")
        self.assertEqual((n["spending"], n["extra"]), (145_000, 20_000))
        left = core.quarter_left(self.s, "Fall 2026", "2026-12-01")
        self.assertEqual(left["spending"], 145_000)

    def test_v6_files_upgrade(self):
        old = dict(self.plain, version=6,
                   categories=[{k: v for k, v in c.items() if k != "extra"} for c in self.plain["categories"]])
        new = core.validate(core.migrate(old))
        self.assertEqual([c["extra"] for c in new["categories"]], [False, False])


class Reoptimize(unittest.TestCase):
    """Food's budget is $500. Optimizing adds $150; a new $130 expense later puts
    the month over, and re-optimizing takes it back: Food ends at $520."""

    def setUp(self):
        s = core.new_budget_from_months(65_000 * 3, "2026-10", "2026-12")  # $650 a month, $0 tuition
        s = core.add_category(s, "Food", 50_000)
        self.s = core.apply_optimize(s)

    def food(self, s, i=0):
        return core.summarize(s)["months"][i]["categories"][0]

    def test_extra_is_even_across_the_quarter(self):
        # Oct, Nov, Dec are all Fall: $150 + $150 + $150 free, less a $130 Oct expense,
        # pools to $320 and splits evenly. Oct's allocation grows to fit.
        s = core.apply_optimize(core.add_item(self.s, "Concert", "2026-10", 13_000))
        self.assertEqual([self.food(s, i)["amount"] for i in range(3)], [60_667, 60_667, 60_666])
        self.assertEqual([m["alloc"] for m in s["months"]], [50_000 + 13_000 + 10_667, 60_667, 60_666])
        self.assertTrue(all(m["remainder"] == 0 for m in core.summarize(s)["months"]))
        assert_balanced(self, s)

    def test_the_example_with_the_rest_of_the_quarter_locked(self):
        # Locked months keep their allocation, so Oct only has its own free money:
        # Food $500 optimized to $650, a $130 expense, re-optimize: $520.
        s = core.set_locked(core.set_locked(self.s, 1, True), 2, True)
        s = core.apply_optimize(core.add_item(s, "Concert", "2026-10", 13_000))
        f = self.food(s)
        self.assertEqual((f["base"], f["boost"], f["amount"]), (50_000, 2_000, 52_000))
        self.assertEqual([m["alloc"] for m in s["months"]], [65_000, 65_000, 65_000])
        self.assertEqual(self.food(s, 1)["amount"], 65_000)
        assert_balanced(self, s)

    def test_never_below_your_budget(self):
        s = core.add_item(self.s, "Trip", "2026-10", 50_000)  # more than the whole quarter's extra
        v = core.summarize(s)["optimize"]
        self.assertEqual(v["still_over"], [{"label": "Oct 2026", "by": 35_000}])
        s = core.apply_optimize(s)
        self.assertEqual([self.food(s, i)["boost"] for i in range(3)], [0, 0, 0])
        self.assertEqual([m["alloc"] for m in s["months"]], [65_000, 65_000, 65_000])
        self.assertTrue(core.summarize(s)["months"][0]["over"])

    def test_extra_grows_back_when_room_returns(self):
        s = core.apply_optimize(core.add_item(self.s, "Concert", "2026-10", 13_000))
        s = core.apply_optimize(core.delete_item(s, "i1"))
        self.assertEqual(self.food(s)["amount"], 65_000)

    def test_preview_shows_take_back(self):
        s = core.add_item(self.s, "Concert", "2026-10", 13_000)
        v = core.summarize(s)["optimize"]
        self.assertEqual((v["take"], v["give"], v["pending"]), (13_000, 0, True))
        self.assertEqual(v["months"][0]["current"], {"c1": 15_000})
        self.assertEqual(v["months"][0]["shares"], {"c1": 10_667})
        self.assertEqual([r["move"] for r in v["months"]], [8_667, -4_333, -4_334])

    def test_extra_isnt_a_hard_floor_for_moving_money(self):
        # Oct's $150 extra can be given up when another month borrows from it.
        s = core.set_month_allocation(self.s, 1, 65_000 + 30_000)
        self.assertEqual([m["alloc"] for m in s["months"]], [50_000, 95_000, 50_000])

    def test_changing_your_amount_keeps_the_extra_separate(self):
        s = core.set_override(self.s, 0, "c1", 45_000)
        f = self.food(s)
        self.assertEqual((f["base"], f["boost"], f["amount"]), (45_000, 15_000, 60_000))
        s = core.apply_optimize(s)
        self.assertEqual(self.food(s)["boost"], 16_667)  # ($200 + $150 + $150) / 3

    def test_unoptimize_then_reoptimize(self):
        s = core.clear_optimize(self.s)
        self.assertEqual(self.food(s)["amount"], 50_000)
        self.assertEqual(core.summarize(s)["months"][0]["remainder"], 15_000)
        with self.assertRaises(core.BudgetError):
            core.clear_optimize(s)
        self.assertEqual(core.apply_optimize(s), self.s)
        assert_balanced(self, s)

    def test_unoptimize_undoes_quarter_evening_exactly(self):
        start = core.clear_optimize(self.s)                       # $650 a month, no extra
        s = core.add_item(start, "Concert", "2026-10", 13_000)
        evened = core.apply_optimize(s)
        self.assertEqual([m["alloc"] for m in evened["months"]], [73_667, 60_667, 60_666])
        self.assertEqual([m["shift"] for m in evened["months"]], [8_667, -4_333, -4_334])
        back = core.clear_optimize(evened)
        self.assertEqual([m["alloc"] for m in back["months"]], [65_000, 65_000, 65_000])
        self.assertEqual(back, s)                                  # exactly where it was
        again = core.apply_optimize(back)
        self.assertEqual(again, evened)                            # same result every time
        with self.assertRaises(core.BudgetError):                  # and no drift on a re-run
            core.apply_optimize(again)

    def test_quarter_unallocated_drops_by_each_expense(self):
        start = core.clear_optimize(self.s)
        q0 = core.summarize(start)["quarters"][0]["unallocated"]
        s = core.add_item(start, "Concert", "2026-10", 13_000)
        self.assertEqual(core.summarize(s)["quarters"][0]["unallocated"], q0 - 13_000)
        self.assertEqual(q0, 3 * 15_000)

    def test_v8_months_get_no_shift(self):
        old = dict(self.s, version=8, months=[{k: v for k, v in m.items() if k != "shift"} for m in self.s["months"]])
        new = core.validate(core.migrate(old))
        self.assertEqual([m["shift"] for m in new["months"]], [0, 0, 0])

    def test_v7_months_get_an_empty_extra(self):
        old = dict(self.s, version=7, months=[{k: v for k, v in m.items() if k != "boost"} for m in self.s["months"]])
        new = core.validate(core.migrate(old))
        self.assertEqual([m["boost"] for m in new["months"]], [{}, {}, {}])


class Request(unittest.TestCase):
    """ucsc(): $1,250 a month Sep 2026 to Aug 2027; Winter tuition $5,000 due Dec 16."""

    def setUp(self):
        s = core.add_person(ucsc(), "Mom")
        # $3,000 received for Oct 1 to Nov 30 (61 days)
        self.s = core.add_receipt(s, date="2026-10-01", amount=300_000, payer="p1",
                                  **{"from": "2026-10-01", "to": "2026-11-30"})

    def test_periods_are_whole_months_and_quarters(self):
        ends = core.period_ends(self.s, "2026-10-12")
        labels = [p["label"] for p in ends]
        self.assertEqual(labels[:3], ["End of Oct 2026", "End of Nov 2026", "End of Dec 2026"])
        self.assertEqual(labels[3], "End of Fall 2026")
        self.assertEqual(labels[-1], "End of the term")
        self.assertNotIn("End of Sep 2026", labels)

    def test_starts_are_whole_months_and_quarters(self):
        starts = core.period_starts(self.s, "2026-10-12")
        self.assertEqual([p["label"] for p in starts[:4]],
                         ["Start of Fall 2026", "Start of Oct 2026", "Start of Nov 2026", "Start of Dec 2026"])
        self.assertEqual(starts[0]["date"], "2026-09-01")   # Fall starts with the plan's Sep
        self.assertEqual([p["label"] for p in starts if p["current"] and p["kind"] == "month"], ["Start of Oct 2026"])
        self.assertNotIn("Start of Sep 2026", [p["label"] for p in starts])  # September is over
        self.assertTrue(all(p["date"].endswith("-01") for p in starts))

    def test_quote_through_end_of_quarter(self):
        q = core.request_quote(self.s, "2026-10-12", "2026-12-31")
        # need: 20/31 of Oct + Nov + Dec + Winter tuition due Dec 16
        self.assertEqual(q["need"]["total"], 125_000 * 20 // 31 + 2 * 125_000 + 500_000)
        # left of the payment: 50 of its 61 days fall in the window
        self.assertEqual(q["covered"], 300_000 * 50 // 61)
        self.assertEqual(q["request"], q["need"]["total"] - q["covered"])
        self.assertEqual(q["paid_through"], "2026-11-30")
        self.assertEqual([(g["from"], g["to"]) for g in q["gaps"]], [("2026-12-01", "2026-12-31")])
        self.assertEqual(q["gaps"][0]["need"], 125_000 + 500_000)

    def test_expected_payments_only_when_asked(self):
        s = core.add_receipt(self.s, received=False, date=None, amount=700_000,
                             **{"from": "2026-12-01", "to": "2026-12-31"})
        self.assertEqual(core.request_quote(s, "2026-10-12", "2026-12-31")["paid_through"], "2026-11-30")
        q = core.request_quote(s, "2026-10-12", "2026-12-31", include_expected=True)
        self.assertEqual((q["paid_through"], q["gaps"], q["request"]), ("2026-12-31", [], 0))
        self.assertGreater(q["extra"], 0)

    def test_parts_add_up_to_the_plan(self):
        s = core.add_category(self.s, "Rent", 90_000)
        s = core.add_item(s, "Cleats", "2026-12", 6_200)
        parts, total = core.need_parts(s, "2026-12-01", "2026-12-31")
        got = {p["label"]: p["amount"] for p in parts}
        self.assertEqual(got, {"Tuition": 500_000, "Rent": 90_000, "Individual expenses": 6_200,
                               "Unallocated money": 125_000 - 90_000 - 6_200})
        self.assertEqual(total, 125_000 + 500_000)

    def test_leave_out_tuition(self):
        with_t = core.request_quote(self.s, "2026-10-12", "2026-12-31")
        without = core.request_quote(self.s, "2026-10-12", "2026-12-31", exclude=["tuition"])
        self.assertEqual(without["need"]["total"], with_t["need"]["total"] - 500_000)
        self.assertEqual(without["need"]["excluded"], 500_000)
        self.assertEqual(without["request"], with_t["request"] - 500_000)
        self.assertEqual(without["gaps"][0]["need"], 125_000)       # December without tuition
        tuition = next(p for p in without["need"]["parts"] if p["key"] == "tuition")
        self.assertFalse(tuition["included"])

    def test_leave_out_a_category(self):
        s = core.add_category(self.s, "Rent", 90_000)
        q = core.request_quote(s, "2026-12-01", "2026-12-31", exclude=["cat:c1", "tuition"])
        self.assertEqual(q["need"]["total"], 125_000 - 90_000)

    def test_nothing_paid_means_whole_window_is_a_gap(self):
        q = core.request_quote(ucsc(), "2026-10-12", "2026-10-31")
        self.assertIsNone(q["paid_through"])
        self.assertEqual(q["request"], q["need"]["total"])
        self.assertEqual(q["gaps"][0]["from"], "2026-10-12")
        with self.assertRaises(core.BudgetError):
            core.request_quote(ucsc(), "2026-10-12", "2026-10-01")


if __name__ == "__main__":
    unittest.main()
