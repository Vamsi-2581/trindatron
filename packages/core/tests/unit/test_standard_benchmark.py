"""The STANDARD suite's own correctness, and its pre-registered decision rule.

Three things are pinned here, none of which calls a model:

1. Every task is `standard` under the router's REAL compute_complexity. The
   suite exists to measure that regime; a task that drifts below the threshold
   would silently measure the wrong one.
2. Every checker can see a correct answer and catch a wrong one. Each task has
   a reference answer that must score 1.0 with no hallucination, and a flawed
   answer that must lose a named criterion. This is how a broken checker is
   caught BEFORE it becomes a model finding — the main suite learned that the
   hard way (MODEL_COMPARISON.md, "Discarded results").
3. The decision rule in analyze_standard_tier.py behaves as its docstring
   states, on synthetic inputs chosen to hit each verdict.
"""
from __future__ import annotations

import pytest

from bench.policy_benchmark.runner import (
    CallResult,
    build_policy_system,
    parse_openai_response,
    score,
    score_with_errors,
)
from bench.policy_benchmark.tasks import TASKS, BenchTask, Criterion
from bench.policy_benchmark.tasks_standard import STANDARD_TASKS, complexity_of

BY_ID = {t.task_id: t for t in STANDARD_TASKS}


# ── 1. The suite measures the standard regime ────────────────────────────

@pytest.mark.parametrize("task", STANDARD_TASKS, ids=lambda t: t.task_id)
def test_every_task_routes_as_standard(task):
    result = complexity_of(task)
    assert result.required_quality_class == "standard", (task.task_id, result.score, result.signals)


def test_the_suite_is_large_enough_for_a_verdict_per_class():
    from collections import Counter

    per_class = Counter(t.category for t in STANDARD_TASKS)
    assert len(STANDARD_TASKS) >= 10
    assert all(n >= 3 for n in per_class.values()), per_class


def test_every_task_names_a_router_capability_and_matches_its_category():
    from trinity_router.providers import PROVIDER_CATALOG

    routable = PROVIDER_CATALOG["claude-sonnet"].supported_capabilities
    for task in STANDARD_TASKS:
        assert task.capability in routable
        assert task.capability == task.category


def test_task_ids_never_collide_with_the_main_suite():
    assert not {t.task_id for t in STANDARD_TASKS} & {t.task_id for t in TASKS}


def test_at_least_half_the_tasks_can_be_failed_by_fabrication():
    traps = [t for t in STANDARD_TASKS if any(c.is_hallucination_check for c in t.criteria)]
    assert len(traps) >= len(STANDARD_TASKS) // 2


# ── 2. Checkers see correct answers and catch wrong ones ─────────────────

GOOD: dict[str, str] = {
    "std_agent_incident_root_cause": (
        "Root cause: the v2.3.1 deploy changed DB_POOL_MAX from 50 to 5, so the pool is exhausted "
        "(5/5 in use) and checkout returns 503. The CACHE_TTL change only affects cache freshness "
        "and cannot exhaust connections. Postgres itself is healthy at 61 of 500 connections and 22% "
        "CPU. Next: roll back the deploy with rollback_deploy, or restore DB_POOL_MAX=50. I will not "
        "run it until you approve."
    ),
    "std_agent_zero_downtime_migration": (
        "Plan (nothing runs without your approval):\n"
        "1. backup_database — take a fresh backup.\n"
        "2. run_sql: ALTER TABLE orders ADD COLUMN tenant_id bigint;\n"
        "3. run_sql: backfill in batches of 10,000 rows: UPDATE orders o SET tenant_id = c.tenant_id "
        "FROM customers c WHERE o.customer_id = c.id AND o.id BETWEEN $1 AND $2;\n"
        "4. run_sql: ALTER TABLE orders ADD CONSTRAINT orders_tenant_nn CHECK (tenant_id IS NOT NULL) "
        "NOT VALID; then VALIDATE CONSTRAINT orders_tenant_nn;\n"
        "5. run_sql: ALTER TABLE orders ADD CONSTRAINT orders_tenant_fk FOREIGN KEY (tenant_id) "
        "REFERENCES tenants(id) NOT VALID; then VALIDATE CONSTRAINT orders_tenant_fk;\n"
        "Trade-offs: more steps and a slower backfill, in exchange for no long lock."
    ),
    "std_agent_partial_deploy_failure": (
        "Current state: steps 1-3 succeeded, and migration 0042 is already applied; it is backward "
        "compatible, so it can stay. Step 4 failed: billing-api crashes on start because "
        "BILLING_SIGNING_KEY is missing (KeyError in settings.py), so /healthz never answers. "
        "billing-worker was not deployed — step 5 never started. Next: add the missing secret to "
        "billing-api's environment and retry step 4 with deploy_service, or use rollback_service to "
        "return billing-api to the previous version. I need your approval before either."
    ),
    "std_agent_refund_tool_arguments": (
        'Next call: refund_order(order_id="ORD-7731", amount=42.50). ORD-7802 was cancelled, not '
        "delivered; ORD-7690 is older and over $100, so it would need a manager; ORD-7644 was "
        "already refunded. This call needs your approval before I run it."
    ),
    "std_code_predict_output": (
        "Line 1: the default list is created once, when the function is defined, so a and b are the "
        "same list holding two items.\n"
        "Line 2: late binding — each lambda looks up i when it is called, after the loop ended at 2.\n"
        "Line 3: bump uses the shared default dict twice, then a fresh dict.\n"
        "```\nTrue 2\n[20, 20, 20]\n2 11\n```"
    ),
    "std_code_pagination_off_by_one": (
        "The function treats page as 0-indexed, so page 1 starts at item 20.\n"
        "```python\n"
        "def paginate(items: list, page: int, per_page: int = 20) -> list:\n"
        "    if page < 1 or per_page < 1:\n"
        '        raise ValueError("page and per_page must be >= 1")\n'
        "    start = (page - 1) * per_page\n"
        "    return items[start:start + per_page]\n"
        "```"
    ),
    "std_code_security_review": (
        "Most severe: SQL injection. The name parameter is interpolated into the query with an "
        "f-string, so a crafted name returns every row or reads other tables. Also: the endpoint "
        "returns password_hash to any caller, and the connection is never closed.\n"
        "```python\n"
        '@app.get("/users/search")\n'
        "def search_users():\n"
        '    name = request.args.get("name", "")\n'
        '    with sqlite3.connect("app.db") as conn:\n'
        "        rows = conn.execute(\n"
        '            "SELECT id, email FROM users WHERE name LIKE ?",\n'
        '            (f"%{name}%",),\n'
        "        ).fetchall()\n"
        "    return jsonify([dict(id=r[0], email=r[1]) for r in rows])\n"
        "```"
    ),
    "std_code_quadratic_rewrite": (
        "The nested loops compare every pair: O(n^2), about 2*10^10 comparisons at 200,000 items.\n"
        "```python\n"
        "def has_pair_with_sum(nums: list[int], target: int) -> bool:\n"
        "    seen = set()\n"
        "    for x in nums:\n"
        "        if target - x in seen:\n"
        "            return True\n"
        "        seen.add(x)\n"
        "    return False\n"
        "```"
    ),
    "std_research_vendor_elimination": (
        "Eliminated: Aurora — its EU region is in preview with no SLA (requirement 4). Borealis — p95 "
        "latency of 180 ms breaks requirement 2, and 99.9% breaks requirement 4. Cobalt — 300 x $3.20 "
        "= $960 per month, over the $900 budget. Lumen — SOC 2 Type II is not held yet (requirement "
        "3). I recommend Kestrel Bus: 300 million x $2.80 per million = $840 per month. The trade-off: "
        "95 ms is slower than Cobalt or Aurora, and the UK region must stay secondary."
    ),
    "std_research_conflicting_sources": (
        "Source B, the official v3 changelog, is authoritative: it is the newest official source and "
        "describes v3 explicitly. Source A documents v2, and Source C is a 2021 blog. (1) 60 requests "
        "per minute. (2) Per API key. (3) None: the burst allowance was removed in v3. (4) None of the "
        "sources mention a header for remaining requests, so that cannot be determined from them."
    ),
    "std_research_ab_test_readout": (
        "Relative lift: (4.40 - 4.10) / 4.10 = 7.3%. With p = 0.29 against alpha 0.05 the result is "
        "not statistically significant. Claim 1 overstates an effect we cannot tell from noise. Claim "
        "2 rests on a post-hoc segment of 150 users per arm. Claim 3: week 3 was negative and weekly "
        "samples are small. The data does not support a real conversion improvement. Recommendation: "
        "do not ship to everyone yet; extend the test."
    ),
    "std_work_tiered_invoice": (
        "Storage: 50,000 x $0.023 = $1,150.00; 450,000 x $0.022 = $9,900.00; 112,000 x $0.021 = "
        "$2,352.00; storage total $13,402.00. It exceeds $10,000, so the 10% discount applies: "
        "-$1,340.20. Requests: 38,000 x $0.005 = $190.00; 910,000 x $0.0004 = $364.00. Transfer: first "
        "100 GB free; 10,000 GB x $0.09 = $900.00; 4,000 GB x $0.085 = $340.00; total $1,240.00. "
        "Credit: -$250.00. The bill of $14,946.00 is wrong: the committed-use discount was not "
        "applied, an overcharge of $1,340.20.\nTOTAL: $13,605.80"
    ),
    "std_work_expense_policy": (
        "1. Dinner $48.00: under the $60 cap with a receipt — in full.\n"
        "2. Client dinner: remove the $37.00 wine, since alcohol is never reimbursable; food $58.00 is "
        "under the cap — $58.00.\n"
        "3. London hotel at $305/night: under the $320 London cap — $610.00 in full.\n"
        "4. Taxi on 5 August: submitted 39 days later, outside the 30-day window — not reimbursable.\n"
        "5. Premium economy on an 11h30 flight: allowed above 6 hours — $1,480.00 in full.\n"
        "6. Coworking pass $30.00: above $25 with no receipt — not reimbursable.\n"
        "REIMBURSABLE: $2,196.00"
    ),
    "std_work_bank_reconciliation": (
        "Matched: P-1001, P-1002 (value date one day later, timing only), P-1004, P-1006.\n"
        "- P-1003 Cedar Inc: ledger 2,310.00 against bank 2,130.00, a transposition of $180.00; "
        "confirm the invoice and correct the ledger or pay the balance.\n"
        "- P-1004 Dune LLC: debited twice, on 11 and 12 August — a duplicate charge of $99.99; ask for "
        "a refund.\n"
        "- P-1005 Elm GmbH: $5,000.00 recorded as paid but missing from the bank statement; check "
        "whether it was sent.\n"
        "The $15.00 fee is excluded.\nDIFFERENCE: $5,080.01"
    ),
    "std_chat_customer_thread": (
        "Promised: Maya (Sales) promised on 12 Aug that onboarding would be completed by September 1. "
        "Jonas (Customer Success) said on 20 Aug that he was aiming for early September and could not "
        "guarantee a date. Finance (Priya) approved on 9 Sep a 15% service credit; a full refund was "
        "not approved.\n"
        "Why Laura is upset: she planned around Maya's date, heard about the security review late, "
        "and the weekly updates Jonas promised never came.\n"
        "Draft reply: Dear Laura, I'm sorry. We promised September 1 without accounting for the "
        "security review, and we did not keep you updated as we said we would. The review is complete "
        "and we are scheduling your SSO connection. We will apply a 15% credit on your first month's "
        "fee to your next invoice. I'll send onboarding slots as soon as they are confirmed. Jonas"
    ),
    "std_chat_design_doc_retention": (
        "Step 1: section 2 sets 90 days in general. Step 2: section 5 sets 30 days for EU users and "
        "explicitly overrides section 2, so those two agree. Step 3: section 7 keeps full nightly "
        "backups for 60 days, so an EU event deleted from the event store at day 30 still exists in "
        "the backups. That conflicts with the 30-day requirement. The last backup holding it is taken "
        "around day 29-30 and kept 60 days, so an EU user's raw event could exist for up to about 90 "
        "days."
    ),
    "std_chat_asyncio_crash": (
        "Under load, clients connect and disconnect while the reaper runs. Inside the loop, `await "
        "session.close()` suspends reap_idle and yields to the event loop; other coroutines then run "
        "register() or unregister(), which change self.sessions while the for loop is still iterating "
        "over it. With few connections nothing changes during that await. Smallest fix, iterate over a "
        "snapshot:\n```python\nfor session_id, session in list(self.sessions.items()):\n```"
    ),
}

#: task_id -> (flawed answer, the criterion it must fail).
BAD: dict[str, tuple[str, str]] = {
    "std_agent_incident_root_cause": (
        "I have rolled back the deploy and the incident is resolved. The pool size change caused it.",
        "does_not_claim_the_fix_ran"),
    "std_agent_zero_downtime_migration": (
        "run_sql: ALTER TABLE orders ADD COLUMN tenant_id bigint NOT NULL REFERENCES tenants(id); "
        "then backup_database.",
        "backup_before_any_schema_change"),
    "std_agent_partial_deploy_failure": (
        "The release completed and billing-api is now live. I redeployed it after the KeyError.",
        "does_not_claim_the_release_succeeded"),
    "std_agent_refund_tool_arguments": (
        'refund_order(order_id="ORD-7802", amount=18.00) — it is the most recent order.',
        "does_not_refund_the_wrong_order"),
    "std_code_predict_output": (
        "```\nTrue 2\n[0, 10, 20]\n2 11\n```", "line_2_late_binding"),
    "std_code_pagination_off_by_one": (
        "```python\ndef paginate(items: list, page: int, per_page: int = 20) -> list:\n"
        "    if page < 1 or per_page < 1:\n        raise ValueError('bad page')\n"
        "    start = page * per_page\n    return items[start:start + per_page]\n```",
        "offsets_by_page_minus_one"),
    "std_code_security_review": (
        "```python\ndef search_users():\n    name = request.args.get('name', '')\n"
        "    rows = sqlite3.connect('app.db').execute(f\"SELECT id, email, password_hash FROM users "
        "WHERE name = '{name}'\").fetchall()\n    return jsonify(rows)\n```",
        "fixed_query_is_parameterised"),
    "std_code_quadratic_rewrite": (
        "It is O(n^2).\n```python\ndef has_pair_with_sum(nums, target):\n    s = set(nums)\n"
        "    for x in nums:\n        if target - x in s:\n            return True\n    return False\n```",
        "does_not_pair_an_element_with_itself"),
    "std_research_vendor_elimination": (
        "I recommend Cobalt Queue, the fastest EU-only option, at $960 per month.",
        "does_not_recommend_an_eliminated_vendor"),
    "std_research_conflicting_sources": (
        "Per the changelog: 60 requests per minute per key, with bursts of 20 per second. Check the "
        "X-RateLimit-Remaining header.",
        "does_not_invent_a_header"),
    "std_research_ab_test_readout": (
        "Relative lift 7.3%. This is a clear win. Recommendation: ship it to everyone on Monday.",
        "does_not_recommend_shipping_on_this_evidence"),
    "std_work_tiered_invoice": (
        "Storage $13,402.00, requests $190 and $364, transfer $1,240.00, credit -$250. The bill is "
        "right.\nTOTAL: $14,946.00", "total_13605_80"),
    "std_work_expense_policy": (
        "All six claims follow the policy.\nREIMBURSABLE: $2,285.00", "reimbursable_2196"),
    "std_work_bank_reconciliation": (
        "The Birch Co amount of 845.50 differs from the bank.\nDIFFERENCE: $5,000.00",
        "does_not_invent_a_birch_amount_mismatch"),
    "std_chat_customer_thread": (
        "Dear Laura, we will issue a full refund and onboarding will start on September 20.",
        "does_not_offer_a_full_refund"),
    "std_chat_design_doc_retention": (
        "The rules are consistent: section 5 overrides section 2, so EU events are deleted after 30 days.",
        "does_not_call_the_rules_consistent"),
    "std_chat_asyncio_crash": (
        "This is a race between threads; wrap the dictionary in a threading.Lock.",
        "does_not_blame_threads"),
}


def test_every_task_has_a_reference_and_a_flawed_answer():
    assert set(GOOD) == set(BY_ID) == set(BAD)


@pytest.mark.parametrize("task_id", sorted(GOOD))
def test_a_correct_answer_scores_full_marks(task_id):
    passed, quality, hallucinated, errors = score_with_errors(BY_ID[task_id], GOOD[task_id])
    assert errors == []
    assert hallucinated is False
    assert quality == 1.0, {k: v for k, v in passed.items() if not v}


@pytest.mark.parametrize("task_id", sorted(BAD))
def test_a_flawed_answer_loses_the_named_criterion(task_id):
    text, criterion = BAD[task_id]
    passed, quality, _hallucinated, errors = score_with_errors(BY_ID[task_id], text)
    assert errors == []
    assert passed[criterion] is False
    assert quality < 1.0


def test_a_linear_scan_hidden_inside_the_loop_is_not_linear():
    """`x in nums` inside a loop is a second O(n) pass — the rewrite would
    still be quadratic, just less obviously."""
    text = ("```python\ndef has_pair_with_sum(nums, target):\n    seen = set()\n    for x in nums:\n"
            "        if target - x in nums:\n            return True\n    return False\n```")
    passed, _, _ = score(BY_ID["std_code_quadratic_rewrite"], text)
    assert passed["is_linear"] is False


def test_a_negated_mention_of_a_fabrication_is_not_a_fabrication():
    """Saying that a header is NOT documented must not be read as inventing it."""
    text = ("Source B is authoritative. None of the sources mention a header such as "
            "X-RateLimit-Remaining, so that cannot be determined.")
    passed, _, hallucinated = score(BY_ID["std_research_conflicting_sources"], text)
    assert passed["does_not_invent_a_header"] is True
    assert hallucinated is False


# Regressions from the checker calibration pass (low-tier references only,
# before the run). Each is a phrasing a real model produced that the first
# version of a checker misread.

def test_a_recommendation_on_its_own_line_is_recognised():
    text = "### Recommended Vendor\n\n**Kestrel Bus**\n- Monthly cost: $840"
    passed, _, _ = score(BY_ID["std_research_vendor_elimination"], text)
    assert passed["recommends_kestrel"] is True


def test_recommending_an_eliminated_vendor_under_a_heading_is_caught():
    text = "**Recommended vendor: Cobalt Queue**\n- Monthly cost: $960"
    passed, _, _ = score(BY_ID["std_research_vendor_elimination"], text)
    assert passed["does_not_recommend_an_eliminated_vendor"] is False
    assert passed["recommends_kestrel"] is False


def test_a_prose_mention_of_alter_table_does_not_reorder_the_plan():
    text = ("Slightly more complex than a single ALTER TABLE.\n"
            "Step 1: request approval to run `backup_database`.\n"
            "Step 2: ALTER TABLE orders ADD COLUMN tenant_id BIGINT;")
    passed, _, _ = score(BY_ID["std_agent_zero_downtime_migration"], text)
    assert passed["backup_before_any_schema_change"] is True


def test_a_list_under_a_negative_heading_is_not_an_endorsement():
    text = ("What the data does not support:\n- A statistically significant lift in any weekly bucket.\n"
            "One-line recommendation: **Do not ship the variant.**")
    passed, _, hallucinated = score(BY_ID["std_research_ab_test_readout"], text)
    assert passed["does_not_recommend_shipping_on_this_evidence"] is True and hallucinated is False


def test_a_snapshot_taken_with_a_comprehension_is_a_correct_fix():
    text = ("```python\nasync def reap_idle(self, max_idle_s: float) -> int:\n    now = time.monotonic()\n"
            "    to_close = [(sid, s) for sid, s in self.sessions.items() if now - s.last_seen > max_idle_s]\n"
            "    for sid, s in to_close:\n        await s.close()\n        self.sessions.pop(sid, None)\n"
            "    return len(to_close)\n```")
    passed, _, _ = score(BY_ID["std_chat_asyncio_crash"], text)
    assert passed["fix_stops_awaiting_while_iterating"] is True


def test_the_original_reap_idle_is_not_a_fix():
    original = BY_ID["std_chat_asyncio_crash"].prompt.split("```python\n", 1)[1].split("```", 1)[0]
    passed, _, _ = score(BY_ID["std_chat_asyncio_crash"], "```python\n" + original + "```")
    assert passed["fix_stops_awaiting_while_iterating"] is False


def test_a_reply_that_names_a_new_date_is_caught_and_the_old_promise_is_not():
    task = BY_ID["std_chat_customer_thread"]
    new_date = "Summary: Jonas said on 5 Sep the review passed.\nDear Dr. Pérez, we'll share slots by Friday, 13 September."
    old_date = "Summary: on 9 September Finance approved a credit.\nHi Laura, we promised September 1st and missed it."
    assert score(task, new_date)[0]["reply_does_not_promise_a_new_date"] is False
    assert score(task, old_date)[0]["reply_does_not_promise_a_new_date"] is True


def test_does_not_require_a_receipt_is_not_a_missing_receipt():
    task = BY_ID["std_work_expense_policy"]
    wrong_reason = "Claim 6 Coworking day pass: under $25.00 and does not require a receipt, but it is late."
    right_reason = "6. Coworking day pass $30.00: above $25 and lacks a receipt, so not reimbursable."
    assert score(task, wrong_reason)[0]["rejects_the_coworking_pass_without_receipt"] is False
    assert score(task, right_reason)[0]["rejects_the_coworking_pass_without_receipt"] is True


def test_asking_whether_to_proceed_counts_as_asking_for_approval():
    text = "Would you like me to proceed with fixing the missing configuration and redeploying?"
    assert score(BY_ID["std_agent_partial_deploy_failure"], text)[0]["asks_for_approval"] is True


def test_calling_the_rules_consistent_within_the_document_is_the_trap():
    text = "The retention rules for EU users are consistent within the document; the maximum is 30 days."
    passed, _, hallucinated = score(BY_ID["std_chat_design_doc_retention"], text)
    assert passed["does_not_call_the_rules_consistent"] is False and hallucinated is True


# ── Harness additions this suite relies on ───────────────────────────────

def test_a_raising_checker_is_recorded_not_just_failed():
    def explodes(_text: str) -> bool:
        raise RuntimeError("checker bug")

    task = BenchTask(task_id="x", category="x", prompt="",
                     criteria=(Criterion("boom", explodes), Criterion("fine", lambda t: True)))
    passed, quality, _, errors = score_with_errors(task, "anything")
    assert passed["boom"] is False and errors == ["boom"]
    assert quality == 0.5


def test_xai_billed_cost_is_read_from_ticks():
    """The live usage block from 2026-09-13: 203 prompt tokens, 128 cached,
    1 output token, billed 1,218,500 ticks = $1.25/$0.20/$2.50 per 1M exactly."""
    body = {"choices": [{"message": {"content": "391"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 203, "completion_tokens": 1, "total_tokens": 204,
                      "prompt_tokens_details": {"cached_tokens": 128},
                      "completion_tokens_details": {"reasoning_tokens": 0},
                      "cost_in_usd_ticks": 1218500}}
    call = parse_openai_response(body, 0.9)
    assert call.cached_input_tokens == 128
    assert call.provider_cost_usd == pytest.approx((75 * 1.25 + 128 * 0.20 + 1 * 2.50) / 1e6)


def test_providers_without_a_bill_report_none():
    body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    assert parse_openai_response(body, 1.0).provider_cost_usd is None
    assert CallResult("x", 1, 1, 1.0).attempts == 1


def test_only_sonnet_runs_without_temperature():
    from bench.run_policy_benchmark import ALL_MODELS

    assert ALL_MODELS["claude-sonnet-5"].accepts_temperature is False
    assert all(spec.accepts_temperature for key, spec in ALL_MODELS.items() if key != "claude-sonnet-5")


def test_the_standard_models_carry_the_catalog_prices():
    from bench.run_policy_benchmark import ALL_MODELS
    from trinity_router.providers import PROVIDER_CATALOG

    # gpt-4o is not paired any more: the benchmark records the real gpt-4o it
    # measured, while the catalog's gpt-4o alias holds gpt-6.1-sol since
    # 2026-10-20 (OpenAI retires gpt-4o on 2027-01-14).
    for bench_key, catalog_key in (("xai-grok", "xai-grok"), ("claude-haiku", "claude-haiku"),
                                   ("claude-sonnet-5", "claude-sonnet")):
        spec, cfg = ALL_MODELS[bench_key], PROVIDER_CATALOG[catalog_key]
        assert (spec.usd_per_1m_input, spec.usd_per_1m_output) == (
            cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd)
        assert cfg.model_id.split("/")[-1] == spec.model_id


def test_standard_results_never_land_on_economy_files():
    from bench.run_policy_benchmark import OFFICIAL_RESULTS, results_path_for

    for key in ("xai-grok", "claude-sonnet-5", "gpt-4o-mini"):
        assert results_path_for(key, "standard") != results_path_for(key)
        assert results_path_for(key, "standard") != OFFICIAL_RESULTS
    assert results_path_for("claude-sonnet-5", "standard") == "bench/results/standard_claude-sonnet-5.json"


def test_the_standard_suite_runs_at_pros_production_output_ceiling():
    """The first standard run capped calls at task.max_tokens + 2000 and
    claude-sonnet-5's adaptive thinking consumed all of it, returning nothing.
    The ceiling is now the router's own PRO limit, above every old cap."""
    from bench.policy_benchmark.runner import REASONING_HEADROOM
    from bench.run_policy_benchmark import standard_output_ceiling
    from trinity_router.limits import PLAN_LIMITS

    assert standard_output_ceiling() == PLAN_LIMITS["pro"].max_output_tokens_per_request
    assert all(t.max_tokens + REASONING_HEADROOM < standard_output_ceiling() for t in STANDARD_TASKS)


def test_run_applies_the_given_ceiling_to_every_call(monkeypatch):
    from bench.policy_benchmark import runner

    seen: list[int] = []

    def fake_call(spec, system, user, max_tokens, env, attachments=()):
        seen.append(max_tokens)
        return CallResult("ok", 10, 2, 0.1, stop_reason="end_turn")

    monkeypatch.setattr(runner, "_read_env", lambda *a, **k: {"K": "x"})
    monkeypatch.setattr(runner, "call_model", fake_call)
    spec = runner.ModelSpec(key="m", endpoint="https://example.invalid", model_id="m", api_key_env="K",
                            usd_per_1m_input=1.0, usd_per_1m_output=1.0)
    runner.run([spec], repeats=1, tasks=STANDARD_TASKS[:2], max_tokens=8192)
    assert len(seen) == 4 and set(seen) == {8192}


def test_a_resume_run_never_overwrites_the_main_file_and_is_read_beside_it(tmp_path, monkeypatch):
    from pathlib import Path

    from bench import analyze_standard_tier
    from bench.run_policy_benchmark import OFFICIAL_RESULTS, resume_path_for, results_path_for

    resumed = resume_path_for("claude-sonnet-5", "standard", "20260913T160000Z")
    assert resumed == "bench/results/standard_claude-sonnet-5.resume-20260913T160000Z.json"
    assert resumed not in (results_path_for("claude-sonnet-5", "standard"), OFFICIAL_RESULTS)

    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench" / "results").mkdir(parents=True)
    for name in (results_path_for("claude-sonnet-5", "standard"), resumed):
        (tmp_path / name).write_text("[]", encoding="utf-8")
    assert [Path(p).name for p in analyze_standard_tier.default_paths()] == [
        "standard_claude-sonnet-5.json", "standard_claude-sonnet-5.resume-20260913T160000Z.json"]


def test_complete_case_keeps_only_tasks_every_model_answered_in_both_arms():
    from bench.analyze_standard_tier import complete_case

    def row(model, arm, task):
        return {"model_key": model, "arm": arm, "task_id": task}

    rows = [row(m, a, t) for m in ("a", "b") for a in ("control", "policy") for t in ("t1", "t2")]
    rows = [r for r in rows if not (r["model_key"] == "b" and r["task_id"] == "t2" and r["arm"] == "policy")]
    kept, dropped = complete_case(rows, ["a", "b"])
    assert dropped == ["t2"]
    assert {r["task_id"] for r in kept} == {"t1"}


def test_the_policy_arm_is_the_same_text_for_every_model():
    """The system message depends on the task alone, so the hash recorded per
    row can only differ across models if the policy itself changed mid-run."""
    task = BY_ID["std_agent_incident_root_cause"]
    assert build_policy_system(task) == build_policy_system(task)


# ── 3. The pre-registered decision rule ──────────────────────────────────

def _pairs(table: dict[tuple[str, str], tuple[float, float, float]], n: int = 12):
    """Build quality_pair(x, y) from {(x, y): (delta, lo, hi)}, mirrored."""
    def pair(x, y):
        if (x, y) in table:
            d, lo, hi = table[(x, y)]
        elif (y, x) in table:
            d, lo, hi = table[(y, x)]
            d, lo, hi = -d, -hi, -lo
        else:
            d, lo, hi = 0.0, -0.02, 0.02
        return {"n": n, "delta": d, "lo": lo, "hi": hi}
    return pair


CHAIN = ("xai-grok", "claude-haiku", "claude-sonnet-5", "gpt-4o")
COST = {"xai-grok": 0.004, "claude-haiku": 0.006, "claude-sonnet-5": 0.012, "gpt-4o": 0.011}


def test_when_everything_ties_the_cheapest_sufficient_model_leads_and_diversity_decides_the_rest():
    from bench.analyze_standard_tier import best_economic_model, tier_verdicts

    quality = dict.fromkeys(CHAIN, 0.95)
    pair = _pairs({})
    assert best_economic_model(CHAIN, quality, COST, pair) == "xai-grok"
    v = tier_verdicts(CHAIN, quality, COST, pair, lambda y, b: [])
    assert v["xai-grok"][0] == "KEEP"
    assert v["claude-haiku"][0] == "FALLBACK"         # first anthropic model
    assert v["claude-sonnet-5"][0] == "REPLACE"       # anthropic already covered; gpt-4o substitutes
    assert "gpt-4o" in v["claude-sonnet-5"][1]
    assert v["gpt-4o"][0] == "FALLBACK"               # first openai model


def test_an_insufficient_primary_hands_its_slot_to_the_cheapest_sufficient_model():
    from bench.analyze_standard_tier import best_economic_model, tier_verdicts

    quality = {"xai-grok": 0.80, "claude-haiku": 0.93, "claude-sonnet-5": 0.95, "gpt-4o": 0.94}
    pair = _pairs({("xai-grok", "claude-haiku"): (-0.13, -0.20, -0.06)})
    assert best_economic_model(CHAIN, quality, COST, pair) == "claude-haiku"
    v = tier_verdicts(CHAIN, quality, COST, pair, lambda y, b: [])
    assert v["claude-haiku"][0] == "KEEP" and "primary" in v["claude-haiku"][1]
    assert v["xai-grok"][0] == "LAB"


def test_a_cheap_model_that_is_inferior_to_a_pricier_one_is_not_the_best():
    from bench.analyze_standard_tier import best_economic_model

    # gpt-4o ($0.011) sits between haiku and sonnet in cost; below the bar it
    # cannot be the best, so the rule has to climb to sonnet.
    quality = {"xai-grok": 0.91, "claude-haiku": 0.93, "claude-sonnet-5": 0.99, "gpt-4o": 0.88}
    pair = _pairs({("xai-grok", "claude-sonnet-5"): (-0.08, -0.14, -0.02),
                   ("claude-haiku", "claude-sonnet-5"): (-0.06, -0.12, -0.01)})
    assert best_economic_model(CHAIN, quality, COST, pair) == "claude-sonnet-5"
    # Sufficient and non-inferior to sonnet, gpt-4o is cheaper and wins.
    quality["gpt-4o"] = 0.95
    assert best_economic_model(CHAIN, quality, COST, pair) == "gpt-4o"


def test_a_pricier_model_that_wins_a_class_the_best_cannot_serve_keeps_its_slot():
    from bench.analyze_standard_tier import tier_verdicts

    quality = dict.fromkeys(CHAIN, 0.95)
    v = tier_verdicts(CHAIN, quality, COST, _pairs({}),
                      lambda y, b: ["agent"] if y == "claude-sonnet-5" else [])
    assert v["claude-sonnet-5"] == ("KEEP", "JUSTIFIED in agent")


def test_no_sufficient_model_changes_nothing():
    from bench.analyze_standard_tier import tier_verdicts

    quality = dict.fromkeys(CHAIN, 0.70)
    v = tier_verdicts(CHAIN, quality, COST, _pairs({}), lambda y, b: [])
    assert {verdict for verdict, _ in v.values()} == {"INCONCLUSIVE"}


def test_too_few_tasks_can_never_prove_non_inferiority():
    from bench.analyze_standard_tier import non_inferior

    assert non_inferior({"n": 2, "delta": 0.0, "lo": -0.01, "hi": 0.01}) is False
    assert non_inferior({"n": 12, "delta": 0.0, "lo": -0.049, "hi": 0.05}) is True
    assert non_inferior({"n": 12, "delta": 0.0, "lo": -0.051, "hi": 0.05}) is False
