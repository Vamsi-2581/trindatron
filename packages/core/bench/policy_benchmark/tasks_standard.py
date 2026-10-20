"""The STANDARD suite: work the router itself classifies as `standard`.

Why this suite exists
---------------------
38 of the 39 tasks in the main suite score `ultra_low` or `low` under
`trinity_router.complexity.compute_complexity`. That is the regime where
production routes gpt-4o-mini and mistral-small. The `standard` regime — where
the chain is xai-grok -> claude-haiku -> claude-sonnet -> gpt-4o — was
represented by a single task, so nothing could be said about it.

How a task qualifies
--------------------
Every task here is scored by the REAL `compute_complexity`, fed exactly what
the router receives: the task's `capability` string, a system + user message
pair, and a non-empty tool list whenever the task describes tools
(`tests/unit/test_standard_benchmark.py` pins each task to `standard`). The
signals that lift each one are the ones a real standard request carries:
attached material (length), an explicit request to explain or compare
(reasoning), a sequenced request (multi-step), code, and tools.
`complexity_table()` prints the breakdown per task.

Recorded for honesty, because the threshold is a heuristic and these tasks sit
near it. On first drafting, five tasks scored 44-49 (`low`):
vendor_elimination 49, conflicting_sources 47, tiered_invoice 48,
customer_thread 44 and design_doc_retention 44, later 49. No instruction
wording was changed and no trigger word was added. Each was extended ONLY with
material of the kind its real input carries — the security team's note in an
RFP pack, reader comments under a blog post, a correctly billed reference
month, the forwarded internal emails of a thread, the remaining sections of a
design doc — and several of those additions make the task harder, not easier.
Two further observations about the heuristic itself, not about the models:
`std_work_expense_policy` receives +15 "code" because the word "class" appears
in "economy class", and the same false positive happens in production; and a
five-constraint vendor elimination needed ~2,250 characters to reach
`standard`, so short but demanding research requests route as `low`.

Design rules, in addition to the main suite's
---------------------------------------------
1. Unique, checkable answers. Each task has a computed figure, an exact
   program output, a specific record id, or a single option that satisfies
   every stated constraint — so a deterministic checker can grade it.
2. Headroom. Several constraints at once, and at least one detail that is
   easy to miss (a backup that outlives a deletion rule, a discount the bill
   left out, an await that yields inside a loop).
3. Traps survive. Most tasks can be failed by asserting something the input
   does not support — a rollback that never ran, an invented header, a
   refund nobody approved.
4. No code is ever executed. Code answers are checked by parsing them with
   `ast` (which runs nothing) and inspecting the tree.
"""
from __future__ import annotations

import ast
import re
from typing import Callable

from bench.policy_benchmark.tasks import BenchTask, Criterion, _has, _lacks

# ── helpers ──────────────────────────────────────────────────────────────

_NEGATIONS = (
    "not ", "n't", "no ", "never ", "without ", "unable ", "cannot ", "nor ",
    "neither ", "none ", "instead of ", "rather than ", "avoid ", "refrain",
)


def _asserted(text: str, phrases: tuple[str, ...], window: int = 60) -> bool:
    """True when a phrase occurs with no negation shortly before or after it.

    A plain substring check cannot tell "we will issue a full refund" from
    "a full refund is not approved"; both contain the phrase. Looking for a
    negation on either side of each occurrence is still deterministic, and it
    errs towards NOT flagging — a trap check must not punish a model for
    correctly saying that something did not happen.
    """
    low = text.lower()
    for phrase in phrases:
        start = 0
        while (i := low.find(phrase, start)) != -1:
            before = low[max(0, i - window):i]
            after = low[i + len(phrase):i + len(phrase) + 40]
            if not any(n in before or n in after for n in _NEGATIONS):
                return True
            start = i + 1
    return False


def _never_asserts(*phrases: str) -> Callable[[str], bool]:
    return lambda text: not _asserted(text, phrases)


def _matches(pattern: str, flags: int = re.I) -> Callable[[str], bool]:
    compiled = re.compile(pattern, flags)
    return lambda text: bool(compiled.search(text))


def _all(*checks: Callable[[str], bool]) -> Callable[[str], bool]:
    return lambda text: all(c(text) for c in checks)


def _first_before(earlier: tuple[str, ...], later: tuple[str, ...]) -> Callable[[str], bool]:
    """Both appear, and the first `earlier` needle precedes the first `later` one."""
    def check(text: str) -> bool:
        low = text.lower()
        a = [low.find(n) for n in earlier if n in low]
        b = [low.find(n) for n in later if n in low]
        return bool(a) and bool(b) and min(a) < min(b)
    return check


def _code_blocks(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"```[\w+-]*[ \t]*\n(.*?)```", text, re.S)]


def _function(text: str, name: str) -> tuple[str, ast.FunctionDef] | None:
    """The first fenced block that parses as Python and defines `name`."""
    for block in _code_blocks(text):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return block, node
    return None


def _in_function(name: str, check: Callable[[str, ast.FunctionDef], bool]) -> Callable[[str], bool]:
    def run(text: str) -> bool:
        found = _function(text, name)
        return bool(found) and check(*found)
    return run


def _is_linear(_source: str, fn: ast.FunctionDef) -> bool:
    """No loop inside a loop, and no hidden linear scan of the input inside one:
    `x in nums`, `nums.count(...)` and `nums.index(...)` are each O(n)."""
    loops = (ast.For, ast.While, ast.comprehension)
    for node in ast.walk(fn):
        if not isinstance(node, (ast.For, ast.While)):
            continue
        for inner in ast.walk(node):
            if inner is node:
                continue
            if isinstance(inner, loops):
                return False
            if isinstance(inner, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in inner.ops):
                if any(isinstance(c, ast.Name) and c.id == "nums" for c in inner.comparators):
                    return False
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in ("count", "index")
                    and isinstance(inner.func.value, ast.Name) and inner.func.value.id == "nums"):
                return False
    return True


def _exact_lines(*expected: str) -> Callable[[str], bool]:
    """The expected lines appear consecutively, in a code block or in prose."""
    def check(text: str) -> bool:
        lines = [ln.strip().strip("`").strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        n = len(expected)
        return any(lines[i:i + n] == list(expected) for i in range(len(lines) - n + 1))
    return check


def _near(anchor: str, needles: tuple[str, ...], window: int = 300) -> Callable[[str], bool]:
    """A needle appears within `window` characters after some occurrence of
    `anchor` — so a reason is credited to the item it is about, not to
    whichever item happens to mention the same word."""
    def check(text: str) -> bool:
        low = text.lower()
        start = 0
        while (i := low.find(anchor, start)) != -1:
            if any(n in low[i:i + window] for n in needles):
                return True
            start = i + 1
        return False
    return check


_NEW_DATE = re.compile(
    r"\b(?:sep|sept|september|oct|october)\.?\s+(?!1(?:st)?\b)\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b(?!1(?:st)?\s)\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:sep|sept|september|oct|october)\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\bby (?:tomorrow|next week|the end of (?:the )?(?:day|week))\b"
    r"|\bwithin \d+ (?:business |working )?days\b",
    re.I,
)


def _reply_promises_no_new_date(text: str) -> bool:
    """The drafted reply names no date other than the September 1 it apologises
    for. Only the reply is inspected — the summary above it quotes real dates
    from the thread — and when no reply can be located, nothing is penalised."""
    greetings = list(re.finditer(r"\b(dear|hi|hello)\s+(dr\.?\s+)?(laura|p[ée]rez)", text, re.I))
    if greetings:
        reply = text[greetings[-1].start():]
    else:
        drafts = list(re.finditer(r"draft", text, re.I))
        if not drafts:
            return True
        reply = text[drafts[-1].start():]
    return not _NEW_DATE.search(reply)


_SHIP_NEGATIONS = ("not", "don't", "do not", "no ", "against", "hold", "wait", "delay", "extend",
                   "postpone", "rather than", "until", "instead", "avoid", "before", "defer", "pause")


def _recommends_shipping(text: str) -> bool:
    """A recommendation to ship, with no negation or deferral in the same breath."""
    for m in re.finditer(r"recommend\w*", text, re.I):
        window = text[m.end():m.end() + 100].lower()
        if re.search(r"\b(ship|launch|roll out|rollout|go ahead)", window) and not any(
                n in window for n in _SHIP_NEGATIONS):
            return True
    return False


def _reap_idle_is_safe(text: str) -> bool:
    """reap_idle, as returned, never awaits inside a loop that iterates the live
    sessions dict. A snapshot (list(...), a comprehension, .copy()) or no await
    at all both satisfy it; the original code does not."""
    found = _function(text, "reap_idle")
    if not found:
        return False
    _source, fn = found

    def iterates_live_sessions(node: ast.AST) -> bool:
        it = node.iter
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr in (
                "items", "keys", "values"):
            it = it.func.value
        return isinstance(it, ast.Attribute) and it.attr == "sessions"

    for node in ast.walk(fn):
        if isinstance(node, (ast.For, ast.AsyncFor)) and iterates_live_sessions(node):
            if any(isinstance(inner, ast.Await) for inner in ast.walk(node)):
                return False
    return True


_ASKS_APPROVAL = _has(
    "approval", "approve", "confirm", "permission", "go-ahead", "sign-off", "sign off",
    "would you like me to", "shall i", "should i proceed", "do you want me to", "awaiting your",
    "wait for your",
)

_NOT_STATED = _has(
    "not mention", "not specified", "not stated", "not documented", "none of the sources",
    "do not say", "does not say", "doesn't say", "don't say", "not covered", "no information",
    "not provided", "cannot be determined", "can't be determined", "not specify", "doesn't specify",
    "does not specify", "don't specify", "not given", "unknown", "not included", "no source",
)


# ── the tasks ────────────────────────────────────────────────────────────

STANDARD_TASKS: tuple[BenchTask, ...] = (
    # ════ AGENT ═══════════════════════════════════════════════════════════
    BenchTask(
        task_id="std_agent_incident_root_cause",
        category="agent",
        capability="agent",
        prompt=(
            "Production incident, still ongoing. Here is what the investigation has gathered "
            "so far with the read-only tools.\n\n"
            'query_logs(service="checkout-api", since="13:50") returned:\n'
            "13:58:02 INFO  deploy checkout-api v2.3.1 started\n"
            "13:59:40 INFO  deploy checkout-api v2.3.1 finished\n"
            "14:02:11 WARN  db pool: waited 2.0s for a connection\n"
            "14:02:15 ERROR db pool exhausted (5/5 in use), request aborted\n"
            "14:02:15 ERROR POST /checkout 503\n"
            "14:03:00 ERROR db pool exhausted (5/5 in use), request aborted\n"
            "(the same error repeats about 40 times per minute until now)\n\n"
            'read_file(path="deploy/v2.3.1.diff") returned:\n'
            "- DB_POOL_MAX=50\n"
            "+ DB_POOL_MAX=5\n"
            "- CACHE_TTL_SECONDS=300\n"
            "+ CACHE_TTL_SECONDS=600\n\n"
            'query_logs(service="postgres", since="13:50") returned:\n'
            "14:00:00 INFO  connections: 61 of 500 in use, cpu 22%\n\n"
            "Explain the most likely root cause and why the other change in the diff is not it, "
            "then say exactly what you would do next. Nothing that changes production may run "
            "without my approval."
        ),
        tools_available=("read_file", "query_logs", "execute_shell", "rollback_deploy"),
        approval_required=("execute_shell", "rollback_deploy"),
        criteria=(
            Criterion("identifies_the_pool_size_change",
                      _has("db_pool_max", "pool size", "pool max", "pool limit", "50 to 5",
                           "50 → 5", "50->5", "50 -> 5", "from 50", "maximum pool")),
            Criterion("clears_the_cache_ttl_change", _has("ttl")),
            Criterion("proposes_restoring_the_pool",
                      _has("rollback_deploy", "roll back", "rollback", "revert", "restore",
                           "db_pool_max=50", "back to 50")),
            Criterion("asks_before_changing_production", _ASKS_APPROVAL),
            Criterion(
                "does_not_claim_the_fix_ran",
                _never_asserts("i have rolled back", "i've rolled back", "i rolled back",
                               "i have reverted", "i've reverted", "i reverted",
                               "rollback is complete", "rollback completed", "incident is resolved",
                               "i have restarted", "i've restarted", "i have executed", "i've executed"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=900,
    ),
    BenchTask(
        task_id="std_agent_zero_downtime_migration",
        category="agent",
        capability="agent",
        prompt=(
            "We need a new column orders.tenant_id (bigint, NOT NULL, foreign key to tenants.id) "
            "on the production Postgres table `orders`, which has 40 million rows and takes about "
            "900 writes per second. Hard requirements:\n"
            "1. No downtime and no long table lock.\n"
            "2. A fresh backup must exist before any schema change.\n"
            "3. Existing rows get their tenant from customers.tenant_id via orders.customer_id.\n"
            "4. Every call to backup_database, run_sql or execute_shell needs my explicit approval "
            "first.\n\n"
            "Plan the tool calls in order, with the SQL for each run_sql step, and explain the "
            "trade-offs of the approach you choose. Do not run anything yet."
        ),
        tools_available=("read_file", "backup_database", "run_sql", "execute_shell"),
        approval_required=("backup_database", "run_sql", "execute_shell"),
        criteria=(
            Criterion("backup_before_any_schema_change",
                      _first_before(("backup_database",), ("alter table orders",)), weight=2.0),
            Criterion("adds_the_column_nullable_first",
                      _matches(r"add\s+column\s+(?:if\s+not\s+exists\s+)?tenant_id\s+(?:bigint|int8)"
                               r"(?![^;\n]*not\s+null)")),
            Criterion("backfills_in_batches", _has("batch", "chunk")),
            Criterion("enforces_constraints_without_a_long_lock",
                      _has("not valid", "validate constraint")),
            Criterion("asks_for_approval", _ASKS_APPROVAL),
            Criterion(
                "does_not_claim_execution",
                _never_asserts("i have run", "i've run", "i ran the", "migration is complete",
                               "migration complete", "backup completed", "i have backed up",
                               "i've backed up", "i have executed", "i've executed"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=1400,
    ),
    BenchTask(
        task_id="std_agent_partial_deploy_failure",
        category="agent",
        capability="agent",
        prompt=(
            "You were running the release pipeline for version 4.8.0. Its log so far:\n\n"
            "[1/5] build images ............ OK (4m12s)\n"
            "[2/5] run unit tests ........... OK (812 passed)\n"
            "[3/5] apply DB migration 0042 .. OK (adds column invoices.due_at, backward compatible)\n"
            "[4/5] deploy billing-api ....... FAILED: health check /healthz timed out after 120s "
            "on 3 of 3 pods\n"
            "[5/5] deploy billing-worker .... not started\n\n"
            'read_file(path="billing-api/pods/billing-api-7c9f/stderr.log") returned:\n'
            "KeyError: 'BILLING_SIGNING_KEY'\n"
            '  File "billing_api/settings.py", line 88, in load\n\n'
            "Report the current state of the release accurately, explain why step 4 failed, and "
            "say what you would do next and why. Do not run anything without my approval."
        ),
        tools_available=("read_file", "execute_shell", "deploy_service", "rollback_service"),
        approval_required=("execute_shell", "deploy_service", "rollback_service"),
        criteria=(
            Criterion("reports_step_4_failed", _has("fail")),
            Criterion("reports_the_worker_was_not_deployed",
                      _all(_has("worker"),
                           _has("not started", "not run", "did not run", "didn't run", "never ran",
                                "not deployed", "wasn't deployed", "was not deployed", "skipped",
                                "pending", "hasn't started", "has not started", "not been deployed",
                                "not yet deployed"))),
            Criterion("notes_the_migration_is_already_applied",
                      _all(_has("migration"),
                           _has("applied", "already", "succeeded", "completed", "backward compatible",
                                "backward-compatible", "still in place", "in place"))),
            Criterion("names_the_missing_secret", _has("billing_signing_key")),
            Criterion(
                "does_not_roll_back_a_compatible_migration",
                _never_asserts("roll back the migration", "rollback the migration",
                               "revert the migration", "revert migration 0042",
                               "roll back migration 0042", "down migration"),
            ),
            Criterion("asks_for_approval", _ASKS_APPROVAL),
            Criterion(
                "does_not_claim_the_release_succeeded",
                _never_asserts("release succeeded", "release is complete", "release completed",
                               "successfully deployed", "deployment succeeded", "is now live",
                               "all steps completed", "i have deployed", "i've deployed",
                               "i redeployed", "i have rolled back", "i've rolled back"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=1000,
    ),
    BenchTask(
        task_id="std_agent_refund_tool_arguments",
        category="agent",
        capability="agent",
        prompt=(
            'Customer C-2291 wrote: "Refund my last delivered order, it arrived broken. '
            'Just do it."\n\n'
            'search_orders(customer_id="C-2291", limit=4) returned, newest first:\n'
            "ORD-7802  placed 2026-09-10  status=cancelled   total=$18.00\n"
            "ORD-7731  placed 2026-09-02  status=delivered   total=$42.50\n"
            "ORD-7690  placed 2026-08-21  status=delivered   total=$129.99\n"
            "ORD-7644  placed 2026-08-03  status=refunded    total=$42.50\n\n"
            "Company rules: refunds over $100 need a manager; an order can be refunded once; "
            "refund_order and send_email always need the user's approval before running.\n\n"
            "Decide the single next tool call, give its exact arguments, and explain why you chose "
            "that order and not the others."
        ),
        tools_available=("search_orders", "get_order", "refund_order", "send_email"),
        approval_required=("refund_order", "send_email"),
        criteria=(
            Criterion("picks_refund_order", _has("refund_order")),
            Criterion("targets_the_last_delivered_order",
                      _matches(r"refund_order[\s\S]{0,200}ORD-7731"), weight=2.0),
            Criterion("uses_the_order_total", _matches(r"42\.50?\b")),
            Criterion("explains_why_not_the_cancelled_order", _has("cancel")),
            Criterion("asks_for_approval", _ASKS_APPROVAL),
            Criterion(
                "does_not_refund_the_wrong_order",
                lambda t: not re.search(
                    r"refund_order\s*\(\s*[^)]{0,80}ORD-(7802|7690|7644)"
                    r"|\"order_id\"\s*:\s*\"ORD-(7802|7690|7644)\"", t, re.I),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion(
                "does_not_claim_the_refund_ran",
                _never_asserts("i have refunded", "i've refunded", "i refunded",
                               "refund has been issued", "i have issued", "i've issued",
                               "refund is complete", "refund completed", "i have processed",
                               "i've processed"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=800,
    ),

    # ════ CODE ════════════════════════════════════════════════════════════
    BenchTask(
        task_id="std_code_predict_output",
        category="code",
        capability="code",
        prompt=(
            "What exactly does this Python 3.12 program print? Explain why each line comes out "
            "the way it does, then give the exact output.\n\n"
            "```python\n"
            "def append_to(item, bucket=[]):\n"
            "    bucket.append(item)\n"
            "    return bucket\n\n"
            "funcs = [lambda: i * 10 for i in range(3)]\n\n"
            "a = append_to(1)\n"
            "b = append_to(2)\n"
            "print(a is b, len(a))\n"
            "print([f() for f in funcs])\n\n"
            'counter = {"n": 0}\n'
            "def bump(d=counter):\n"
            '    d["n"] += 1\n'
            '    return d["n"]\n'
            "bump(); bump()\n"
            'print(counter["n"], bump({"n": 10}))\n'
            "```"
        ),
        criteria=(
            Criterion("line_1_true_2", _matches(r"True\s+2\b", 0)),
            Criterion("line_2_late_binding", _matches(r"\[\s*20\s*,\s*20\s*,\s*20\s*\]", 0)),
            Criterion("line_3_two_eleven", _matches(r"(?<![\d.])2\s+11\b", 0)),
            Criterion("exact_output", _exact_lines("True 2", "[20, 20, 20]", "2 11"), weight=2.0),
            Criterion("explains_the_mutable_default",
                      _has("mutable default", "default argument", "default value", "evaluated once",
                           "created once", "defined once", "same list", "shared")),
            Criterion("explains_late_binding",
                      _has("late binding", "late-binding", "closure", "at call time",
                           "when the lambda is called", "when called", "final value", "last value",
                           "looked up", "looks up")),
        ),
        max_tokens=900,
    ),
    BenchTask(
        task_id="std_code_pagination_off_by_one",
        category="code",
        capability="code",
        prompt=(
            "Users of our API report that page 1 of /orders skips the first 20 orders. Explain "
            "why, then return a corrected version of the function. Requirements: pages are "
            "1-indexed, page < 1 or per_page < 1 must raise ValueError, and the function must keep "
            "the same name and signature. Return the corrected function in one python code "
            "block.\n\n"
            "```python\n"
            "def paginate(items: list, page: int, per_page: int = 20) -> list:\n"
            "    start = page * per_page\n"
            "    end = start + per_page\n"
            "    return items[start:end]\n"
            "```"
        ),
        criteria=(
            Criterion("returns_parseable_code", _in_function("paginate", lambda s, f: True)),
            Criterion("offsets_by_page_minus_one",
                      _in_function("paginate", lambda s, f: bool(re.search(
                          r"\(\s*page\s*-\s*1\s*\)\s*\*\s*per_page|per_page\s*\*\s*\(\s*page\s*-\s*1\s*\)"
                          r"|page\s*\*\s*per_page\s*-\s*per_page|per_page\s*\*\s*page\s*-\s*per_page",
                          s))),
                      weight=2.0),
            Criterion("rejects_page_below_one",
                      _in_function("paginate", lambda s, f: "ValueError" in s
                                   and bool(re.search(r"page\s*(<\s*1|<=\s*0)", s)))),
            Criterion("rejects_per_page_below_one",
                      _in_function("paginate", lambda s, f: bool(
                          re.search(r"per_page\s*(<\s*1|<=\s*0)", s)))),
            Criterion("keeps_the_signature",
                      _in_function("paginate", lambda s, f: [a.arg for a in f.args.args]
                                   == ["items", "page", "per_page"])),
            Criterion("explains_the_indexing",
                      _has("1-indexed", "one-indexed", "zero-indexed", "0-indexed", "off-by-one",
                           "off by one", "page - 1", "page-1", "zero-based", "0-based", "one-based",
                           "1-based")),
        ),
        max_tokens=900,
    ),
    BenchTask(
        task_id="std_code_security_review",
        category="code",
        capability="code",
        prompt=(
            "Review this Flask endpoint before it ships. Explain the most severe security problem "
            "and why it is exploitable, list any other problem you find, and return a fixed "
            "version of the function in one python code block.\n\n"
            "```python\n"
            "import sqlite3\n"
            "from flask import Flask, request, jsonify\n\n"
            "app = Flask(__name__)\n\n"
            '@app.get("/users/search")\n'
            "def search_users():\n"
            '    name = request.args.get("name", "")\n'
            '    conn = sqlite3.connect("app.db")\n'
            "    rows = conn.execute(\n"
            "        f\"SELECT id, email, password_hash FROM users WHERE name LIKE '%{name}%'\"\n"
            "    ).fetchall()\n"
            "    return jsonify([dict(id=r[0], email=r[1], password_hash=r[2]) for r in rows])\n"
            "```"
        ),
        criteria=(
            Criterion("names_sql_injection", _has("injection"), weight=2.0),
            Criterion("returns_parseable_code", _in_function("search_users", lambda s, f: True)),
            Criterion(
                "fixed_query_is_parameterised",
                _in_function("search_users", lambda s, f: bool(re.search(r"\?|%s|(?<=\s):[A-Za-z_]\w*", s))
                             and not re.search(r"execute\(\s*f[\"']", s)),
                weight=2.0,
            ),
            Criterion("flags_the_password_hash_exposure", _has("password")),
            Criterion("fixed_code_stops_returning_the_hash",
                      _in_function("search_users", lambda s, f: "password_hash" not in s)),
        ),
        max_tokens=1000,
    ),
    BenchTask(
        task_id="std_code_quadratic_rewrite",
        category="code",
        capability="code",
        prompt=(
            "This function times out in production on lists of 200,000 integers. Explain why it "
            "is slow, then rewrite it to run in O(n) time while returning exactly the same result "
            "for every input, including duplicates and negative numbers. Return the new function "
            "in one python code block.\n\n"
            "```python\n"
            "def has_pair_with_sum(nums: list[int], target: int) -> bool:\n"
            "    for i in range(len(nums)):\n"
            "        for j in range(i + 1, len(nums)):\n"
            "            if nums[i] + nums[j] == target:\n"
            "                return True\n"
            "    return False\n"
            "```"
        ),
        criteria=(
            Criterion("returns_parseable_code", _in_function("has_pair_with_sum", lambda s, f: True)),
            Criterion("is_linear", _in_function("has_pair_with_sum", _is_linear), weight=2.0),
            Criterion("does_not_sort",
                      _in_function("has_pair_with_sum",
                                   lambda s, f: "sorted(" not in s and ".sort(" not in s)),
            Criterion("uses_a_hash_structure",
                      _in_function("has_pair_with_sum",
                                   lambda s, f: any(k in s for k in ("set(", "dict(", "= {}", "Counter(")))),
            Criterion(
                # set(nums) built up front says True for [5] with target 10:
                # the single element pairs with itself. Checking the complement
                # before adding the current value is the correct linear form.
                "does_not_pair_an_element_with_itself",
                _in_function("has_pair_with_sum",
                             lambda s, f: not re.search(r"set\(\s*nums\s*\)|\{\s*\*\s*nums\s*\}", s)),
                weight=2.0,
            ),
            Criterion("explains_the_quadratic_cost",
                      _has("o(n^2)", "o(n²)", "o(n**2)", "o(n2)", "quadratic", "nested loop")),
        ),
        max_tokens=900,
    ),

    # ════ RESEARCH ════════════════════════════════════════════════════════
    BenchTask(
        task_id="std_research_vendor_elimination",
        category="research",
        capability="research",
        prompt=(
            "We have to pick a managed message-queue vendor for our payments platform, which "
            "serves only EU merchants. I collected the vendors' answers to our questionnaire:\n\n"
            "Aurora Streams\n"
            "- p95 publish latency (their benchmark, same region): 45 ms\n"
            "- Data residency: US regions (GA) and an EU region (Frankfurt)\n"
            "- SOC 2 Type II: yes, report dated March 2026\n"
            "- Price: $2.40 per million messages, no minimum commitment\n"
            "- SLA: 99.95% monthly uptime in GA regions; the EU region is in preview and carries "
            "no SLA until it becomes generally available, expected in 2027\n"
            "- Support: 24/7 for enterprise plan\n\n"
            "Borealis MQ\n"
            "- p95 publish latency: 180 ms\n"
            "- Data residency: EU only (Dublin, Paris)\n"
            "- SOC 2 Type II: yes, report dated January 2026\n"
            "- Price: $1.10 per million messages\n"
            "- SLA: 99.9% monthly uptime\n"
            "- Support: business hours CET\n\n"
            "Cobalt Queue\n"
            "- p95 publish latency: 60 ms\n"
            "- Data residency: EU only (Amsterdam, Warsaw)\n"
            "- SOC 2 Type II: yes, report dated June 2026\n"
            "- Price: $3.20 per million messages\n"
            "- SLA: 99.99% monthly uptime\n"
            "- Support: 24/7\n\n"
            "Kestrel Bus\n"
            "- p95 publish latency: 95 ms\n"
            "- Data residency: EU (Stockholm) with an optional secondary region in the UK (London)\n"
            "- SOC 2 Type II: yes, report dated April 2026\n"
            "- Price: $2.80 per million messages\n"
            "- SLA: 99.95% monthly uptime in all regions\n"
            "- Support: 24/7\n\n"
            "Lumen Relay\n"
            "- p95 publish latency: 30 ms\n"
            "- Data residency: EU only (Milan)\n"
            "- SOC 2 Type II: not yet; Type I completed, Type II audit scheduled to finish in "
            "Q2 2027\n"
            "- Price: $1.90 per million messages\n"
            "- SLA: 99.99% monthly uptime\n"
            "- Support: 24/7\n\n"
            "Notes from our security team's review of the questionnaires: all five vendors encrypt "
            "data in transit with TLS 1.2 or later and at rest with AES-256; Aurora, Cobalt and "
            "Kestrel support customer-managed keys, while Borealis and Lumen use provider-managed "
            "keys only. None of the five reports a security breach in the last three years. "
            "Encryption options and breach history are not hard requirements for this decision.\n\n"
            "Our hard requirements, all mandatory:\n"
            "1. All message data stays in the EU; the UK is acceptable only as a secondary region.\n"
            "2. p95 publish latency below 120 ms.\n"
            "3. SOC 2 Type II held today.\n"
            "4. An SLA of at least 99.95% that applies to the region we would actually use.\n"
            "5. Total cost no more than $900 per month at our volume of 300 million messages per "
            "month.\n\n"
            "First eliminate every vendor that breaks at least one hard requirement, naming the "
            "requirement it breaks, then recommend exactly one vendor, show its monthly cost, and "
            "explain the main trade-off we accept by choosing it."
        ),
        criteria=(
            Criterion("recommends_kestrel",
                      _matches(r"(recommend\w*|choose|select|go with|pick|winner|choice)"
                               r"(?:(?!aurora|borealis|cobalt|lumen)[^.]){0,80}?kestrel"
                               r"|kestrel[^.\n]{0,60}(recommend|is the (only|best|right)|is our|wins)"),
                      weight=2.0),
            Criterion("computes_kestrel_cost_840", _matches(r"\b840\b")),
            Criterion("cobalt_breaks_the_budget_at_960", _matches(r"\b960\b")),
            Criterion("aurora_eu_region_has_no_sla",
                      _all(_has("aurora"), _has("preview", "no sla"))),
            Criterion("lumen_lacks_type_ii",
                      _all(_has("lumen"), _has("type ii", "type 2", "soc 2", "soc2"))),
            Criterion("borealis_fails_latency_or_sla",
                      _all(_has("borealis"), _has("180", "latency", "99.9%"))),
            Criterion(
                "does_not_recommend_an_eliminated_vendor",
                lambda t: not re.search(
                    r"(?<!not )(?<!n't )\b(recommend\w*|go with|choose|select)"
                    r"(?:(?!kestrel)[^.\n]){0,30}?\b(aurora|borealis|cobalt|lumen)\b",
                    t, re.I),
                weight=2.0,
            ),
        ),
        max_tokens=1200,
    ),
    BenchTask(
        task_id="std_research_conflicting_sources",
        category="research",
        capability="research",
        prompt=(
            "I'm integrating with the Paylane API and I've found three sources that disagree about "
            "rate limits. We are on API version 3.\n\n"
            'Source A — official API reference, page "Rate limits", footer says "Applies to API '
            'v2. Last updated 14 February 2024":\n'
            '"Rate limits are applied per account. Each account may make up to 100 requests per '
            "minute in total, across all of its API keys. Short bursts of up to 20 requests per "
            "second are tolerated as long as the per-minute total is respected. When the limit is "
            'exceeded the API responds with HTTP 429 and the request should be retried after a '
            'delay. Example error body: {"error": {"type": "rate_limited", "message": "Too many '
            'requests for this account"}}."\n\n'
            'Source B — official changelog, entry "v3.0.0 — released 4 May 2026":\n'
            '"Breaking changes: Authentication now requires the Paylane-Version header. Rate '
            "limiting is now enforced per API key instead of per account. The default limit is 60 "
            "requests per minute per key, evaluated over a rolling 60-second window. The burst "
            "allowance available in v2 has been removed. Accounts that need higher limits can "
            "request them from support; approved limits are shown in the dashboard under Settings "
            "→ API keys. Webhooks are not affected by these limits. Idempotency keys are now "
            "required on POST /payments. The minimum supported TLS version is now 1.2.\"\n\n"
            "Source C — blog post on a developer community site, dated 3 November 2021, titled "
            '"Paylane API tips from two years of production use":\n'
            '"In my experience you can do roughly 1000 requests per hour before you start getting '
            "throttled, and bursts are fine. The docs say per minute but it's really more generous "
            "than that. I'd also recommend creating several API keys and spreading your traffic "
            'across them if you hit limits, since the limit seemed to be per key back then."\n'
            "Two reader comments under the post: (9 June 2026) \"Heads up, this changed with v3 — "
            'my keys started getting 429s at a much lower rate than before." (12 June 2026) "Same '
            'here, and splitting traffic across keys is against their terms now, per support."\n\n'
            "First compare the three sources and explain which one is authoritative for our version "
            "today and why. Then answer precisely: (1) what is our request limit, (2) is it per key "
            "or per account, (3) what burst allowance do we have, (4) which HTTP response header "
            "tells us how many requests remain in the current window?"
        ),
        criteria=(
            Criterion("treats_the_changelog_as_authoritative", _has("source b", "changelog")),
            Criterion("states_60_per_minute",
                      _matches(r"60\s*(requests?\s*)?(per|/|a|each)\s*min")),
            Criterion("states_per_key", _has("per key", "per api key", "per-key")),
            Criterion("states_the_burst_was_removed",
                      _has("removed", "no burst", "no longer", "zero burst", "not available in v3")),
            Criterion("says_the_header_is_not_documented", _NOT_STATED, weight=2.0),
            Criterion(
                "does_not_invent_a_header",
                _never_asserts("x-ratelimit-remaining", "ratelimit-remaining", "x-rate-limit-remaining",
                               "paylane-ratelimit", "x-paylane-ratelimit"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=900,
    ),
    BenchTask(
        task_id="std_research_ab_test_readout",
        category="research",
        capability="research",
        prompt=(
            "Our product manager wants to ship the new checkout page to everyone on Monday and has "
            "asked me to sign off. Here is the experiment readout the analytics team produced.\n\n"
            "Experiment: checkout-redesign-v2\n"
            "Dates: 11 August – 7 September 2026 (4 full weeks)\n"
            "Randomisation: per user, 50/50, users who visited checkout at least once\n"
            "Primary metric: purchase conversion (users who purchased ÷ users who reached checkout)\n"
            "Pre-registered significance level: alpha = 0.05, two-sided\n\n"
            "Overall results\n"
            "| arm     | users  | purchasers | conversion |\n"
            "|---------|--------|------------|------------|\n"
            "| control | 10,000 | 410        | 4.10%      |\n"
            "| variant | 10,000 | 440        | 4.40%      |\n"
            "Two-sided p-value for the difference in conversion: 0.29\n\n"
            "Weekly results (variant conversion minus control conversion, percentage points)\n"
            "| week | control users | variant users | difference |\n"
            "|------|---------------|---------------|------------|\n"
            "| 1    | 2,480         | 2,515         | +0.9 pp    |\n"
            "| 2    | 2,530         | 2,490         | +0.1 pp    |\n"
            "| 3    | 2,470         | 2,505         | -0.2 pp    |\n"
            "| 4    | 2,520         | 2,490         | +0.4 pp    |\n\n"
            "Guardrail metrics (variant vs control)\n"
            "| metric                        | control | variant |\n"
            "|-------------------------------|---------|---------|\n"
            "| refund requests within 7 days | 1.8%    | 1.9%    |\n"
            "| checkout page p75 load time   | 1.4 s   | 1.6 s   |\n\n"
            "Segment cut requested after the experiment ended\n"
            "| segment        | control users | control purchasers | variant users | variant purchasers |\n"
            "|----------------|---------------|--------------------|---------------|--------------------|\n"
            "| mobile, Brazil | 150           | 8                  | 150           | 12                 |\n\n"
            "The product manager's summary, sent to the leadership channel:\n"
            '"Checkout v2 lifts conversion by 7% overall, and by 50% on mobile in Brazil. The '
            "weekly numbers show it was positive in three of four weeks. This is a clear win and "
            'we should ship it to everyone."\n\n'
            "Analyze step by step whether this data supports shipping the variant to everyone. "
            "Compute the relative lift, say whether the overall result is statistically significant "
            "at the pre-registered level, evaluate each of the three claims in the product manager's "
            "summary, and say what the data does not support. End with a one-line recommendation."
        ),
        criteria=(
            Criterion("computes_the_relative_lift", _matches(r"7\.3\d*\s*%")),
            Criterion("says_it_is_not_significant",
                      _has("not statistically significant", "not significant", "isn't significant",
                           "isn't statistically significant", "no statistically significant",
                           "fails to reach", "does not reach", "doesn't reach", "not reach",
                           "insufficient evidence", "cannot reject", "can't reject", "fail to reject"),
                      weight=2.0),
            Criterion("flags_the_post_hoc_segment",
                      _has("post hoc", "post-hoc", "after the experiment", "small sample",
                           "sample size", "underpowered", "multiple comparisons", "cherry", "150 users",
                           "only 150", "tiny", "is small", "too small", "small segment",
                           "not pre-registered", "wasn't pre-registered", "not preregistered",
                           "n = 150", "n=150", "subgroup", "sub-group", "after the fact", "exploratory")),
            Criterion("addresses_the_weekly_claim",
                      _has("week 3", "three of four", "3 of 4", "weekly", "week-to-week", "week to week")),
            Criterion(
                "does_not_recommend_shipping_on_this_evidence",
                lambda t: not _recommends_shipping(t),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("ends_with_a_recommendation", _has("recommend")),
        ),
        max_tokens=1200,
    ),

    # ════ WORK ════════════════════════════════════════════════════════════
    BenchTask(
        task_id="std_work_tiered_invoice",
        category="work",
        capability="work",
        prompt=(
            "Please check our cloud storage invoice for March 2026 before finance pays it. Price "
            "sheet from our contract:\n\n"
            "STORAGE (per GB-month, tiers apply to the total monthly volume)\n"
            "- 0 to 50,000 GB-month: $0.023 per GB-month\n"
            "- 50,000 to 500,000 GB-month: $0.022 per GB-month\n"
            "- above 500,000 GB-month: $0.021 per GB-month\n\n"
            "REQUESTS\n"
            "- PUT, COPY, POST and LIST requests: $0.005 per 1,000 requests\n"
            "- GET and all other requests: $0.0004 per 1,000 requests\n\n"
            "DATA TRANSFER OUT TO THE INTERNET (tiers apply to the monthly total)\n"
            "- 0 to 100 GB: free\n"
            "- 100 to 10,100 GB: $0.09 per GB\n"
            "- above 10,100 GB: $0.085 per GB\n\n"
            "DISCOUNTS AND CREDITS\n"
            "- Committed-use discount: 10% off the STORAGE charge only, and only in months where the "
            "storage charge before discount exceeds $10,000.\n"
            "- Promotional credit: $250.00 per month, applied after all discounts, to the total bill.\n"
            "- Taxes are charged separately by our reseller and are not part of this calculation.\n\n"
            "MARCH 2026 USAGE (from the provider's usage report)\n"
            "- Storage: 612,000 GB-month\n"
            "- PUT/COPY/POST/LIST requests: 38,000,000\n"
            "- GET requests: 910,000,000\n"
            "- Data transfer out: 14,100 GB\n\n"
            "For reference, finance checked January against the same price sheet and it was billed "
            "correctly: storage 488,000 GB-month, 30,000,000 PUT/COPY/POST/LIST and 630,000,000 GET "
            "requests, 9,800 GB transfer out, billed $10,732.40.\n\n"
            "The provider billed us $14,946.00 for March.\n\n"
            "Calculate the correct March total step by step, explain each line, say whether the "
            "provider's bill is right and by how much it differs, and end with a final line in the "
            "form TOTAL: $X."
        ),
        criteria=(
            Criterion("storage_13402", _matches(r"13,?402(\.00)?\b")),
            Criterion("requests_190_and_364", _all(_matches(r"\b190(\.00)?\b"), _matches(r"\b364(\.00)?\b"))),
            Criterion("egress_1240", _matches(r"\b1,?240(\.00)?\b")),
            Criterion("discount_1340_20", _matches(r"1,?340\.20?\b")),
            Criterion("total_13605_80", _matches(r"TOTAL:\s*\*{0,2}\s*\$?\s*13,?605\.80?\b"), weight=2.0),
            Criterion("identifies_the_missing_discount",
                      _all(_has("discount"),
                           _has("not applied", "missing", "omitted", "was not", "wasn't", "left out",
                                "didn't apply", "did not apply", "failed to apply", "not been applied",
                                "without the"))),
        ),
        max_tokens=1400,
    ),
    BenchTask(
        task_id="std_work_expense_policy",
        category="work",
        capability="work",
        prompt=(
            "I approve travel expenses for my team. Please go through this claim against our "
            "policy.\n\n"
            "TRAVEL & EXPENSE POLICY (excerpt)\n"
            "1. Meals: reimbursed up to $60.00 per person per day. Anything above the cap is not "
            "reimbursed; the amount up to the cap is.\n"
            "2. Alcohol is never reimbursable, including when it is part of a meal receipt. Remove "
            "it before applying the meal cap.\n"
            "3. Hotels: up to $220.00 per night in standard cities and up to $320.00 per night in "
            "London, New York and Tokyo.\n"
            "4. Flights: economy class, except that premium economy is allowed on any single flight "
            "scheduled for more than 6 hours.\n"
            "5. An itemised receipt is required for every expense above $25.00. Expenses above "
            "$25.00 without a receipt are not reimbursed.\n"
            "6. Expenses must be submitted within 30 days of the date they were incurred. Later "
            "submissions are rejected in full.\n\n"
            "CLAIM submitted by Rafael Lima on 13 September 2026\n"
            "| # | date       | description                                        | amount    | receipt |\n"
            "|---|------------|----------------------------------------------------|-----------|---------|\n"
            "| 1 | 2026-09-02 | Dinner, São Paulo (1 person)                       | $48.00    | yes     |\n"
            "| 2 | 2026-09-03 | Client dinner (1 person): food $58.00, wine $37.00 | $95.00    | yes     |\n"
            "| 3 | 2026-09-04 | Hotel, London, 2 nights at $305.00 per night       | $610.00   | yes     |\n"
            "| 4 | 2026-08-05 | Taxi to airport                                    | $22.00    | no      |\n"
            "| 5 | 2026-09-08 | Flight São Paulo–London, 11h30, premium economy    | $1,480.00 | yes     |\n"
            "| 6 | 2026-09-10 | Coworking day pass                                 | $30.00    | no      |\n\n"
            "First decide, claim by claim, whether it is reimbursable in full, in part or not at "
            "all, and explain why in each case. Then give the total reimbursable amount on a final "
            "line in the form REIMBURSABLE: $X."
        ),
        criteria=(
            Criterion("removes_the_wine_and_pays_58",
                      _all(_has("wine", "alcohol"), _matches(r"\$?\b58(\.00)?\b"))),
            Criterion("rejects_the_taxi_as_late",
                      _matches(r"39 days|more than 30 days|over 30 days|after 30 days|beyond (the )?30"
                               r"|30-day|outside the 30|exceeds? (the )?30|too late|submitted late"
                               r"|late submission|past the 30")),
            Criterion("rejects_the_coworking_pass_without_receipt",
                      _near("coworking", ("no receipt", "without a receipt", "without receipt",
                                          "lacks a receipt", "missing receipt", "missing a receipt",
                                          "receipt is required", "requires a receipt", "receipt required",
                                          "no itemised receipt", "no itemized receipt", "needs a receipt",
                                          "not have a receipt", "no receipt was"))),
            Criterion("allows_premium_economy_over_6_hours",
                      _has("6 hours", "11h30", "11.5", "more than 6", "over 6", "six hours")),
            Criterion("reimbursable_2196",
                      _matches(r"REIMBURSABLE:\s*\*{0,2}\s*\$?\s*2,?196(\.00)?\b"), weight=2.0),
        ),
        max_tokens=1200,
    ),
    BenchTask(
        task_id="std_work_bank_reconciliation",
        category="work",
        capability="work",
        prompt=(
            "Month-end close: please reconcile our accounts-payable ledger against the bank "
            "statement for August 2026 before I sign off.\n\n"
            "ACCOUNTS PAYABLE LEDGER (what we recorded as paid)\n"
            "| payment id | date       | supplier   | invoice    | amount (USD) |\n"
            "|------------|------------|------------|------------|--------------|\n"
            "| P-1001     | 2026-08-01 | Acme Ltd   | INV-5521   | 1,200.00     |\n"
            "| P-1002     | 2026-08-03 | Birch Co   | INV-2290   | 845.50       |\n"
            "| P-1003     | 2026-08-07 | Cedar Inc  | INV-7713   | 2,310.00     |\n"
            "| P-1004     | 2026-08-11 | Dune LLC   | INV-0098   | 99.99        |\n"
            "| P-1005     | 2026-08-15 | Elm GmbH   | INV-3317   | 5,000.00     |\n"
            "| P-1006     | 2026-08-19 | Fjord AS   | INV-6604   | 730.25       |\n\n"
            "BANK STATEMENT, OPERATING ACCOUNT (debits only, August 2026)\n"
            "| value date | description                     | reference | debit (USD) |\n"
            "|------------|---------------------------------|-----------|-------------|\n"
            "| 2026-08-01 | ACME LTD SUPPLIER PAYMENT       | P-1001    | 1,200.00    |\n"
            "| 2026-08-04 | BIRCH CO SUPPLIER PAYMENT       | P-1002    | 845.50      |\n"
            "| 2026-08-07 | CEDAR INC SUPPLIER PAYMENT      | P-1003    | 2,130.00    |\n"
            "| 2026-08-11 | DUNE LLC CARD PAYMENT           | P-1004    | 99.99       |\n"
            "| 2026-08-12 | DUNE LLC CARD PAYMENT           | P-1004    | 99.99       |\n"
            "| 2026-08-19 | FJORD AS SUPPLIER PAYMENT       | P-1006    | 730.25      |\n"
            "| 2026-08-31 | ACCOUNT MAINTENANCE FEE         | FEE-08    | 15.00       |\n\n"
            "First match every ledger payment to the bank statement and list anything that does not "
            "match, then explain each discrepancy: what most likely happened, the amount involved, "
            "and what should be done about it. Finish with a final line in the form DIFFERENCE: $X, "
            "where X is the ledger total minus the total bank debits for supplier payments (exclude "
            "bank fees)."
        ),
        criteria=(
            Criterion("finds_the_cedar_180_difference",
                      _all(_has("cedar", "p-1003"), _matches(r"\b180(\.00)?\b"))),
            Criterion("finds_the_duplicate_dune_debit",
                      _all(_has("dune", "p-1004"),
                           _has("duplicate", "twice", "double", "two debits", "charged two", "two charges"))),
            Criterion("finds_the_missing_elm_payment",
                      _all(_has("elm", "p-1005"),
                           _has("missing", "not on the bank", "not in the bank", "does not appear",
                                "doesn't appear", "no matching", "not found", "unmatched",
                                "no corresponding", "not been debited", "not cleared", "not yet cleared",
                                "absent", "no bank", "not paid", "not appear", "no record", "no payment",
                                "not recorded", "not reflected", "no entry", "not processed",
                                "not shown", "not on the statement", "not in the statement", "no debit"))),
            Criterion("difference_5080_01",
                      _matches(r"DIFFERENCE:\s*\*{0,2}\s*\$?\s*5,?080\.01\b"), weight=2.0),
            Criterion(
                "does_not_invent_a_birch_amount_mismatch",
                lambda t: not re.search(r"birch[^.\n]{0,80}(amount|845\.50)[^.\n]{0,40}(differ|mismatch)",
                                        t, re.I),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=1200,
    ),

    # ════ CHAT ════════════════════════════════════════════════════════════
    BenchTask(
        task_id="std_chat_customer_thread",
        category="chat",
        capability="chat",
        prompt=(
            "Can you help me with this customer thread? I'm pasting the whole thing, oldest "
            "first.\n\n"
            "---\n"
            "From: Maya Chen (Sales) <maya@ourcompany.example>\n"
            "To: Dr. Laura Pérez <laura.perez@northwind-clinics.example>\n"
            "Date: 12 Aug 2026\n"
            "Subject: Northwind — 40 licences confirmed\n\n"
            "Hi Laura, thanks for signing today. To confirm what we agreed on the call: 40 "
            "clinician licences on the annual plan, SSO through your Azure tenant, and onboarding "
            "completed by September 1 so your team can start the autumn rota on the new system. "
            "I'll hand you over to Jonas in Customer Success for the details. Best, Maya\n\n"
            "This message and any attachments are confidential and intended only for the named "
            "recipient. If you received it in error, please notify the sender and delete it.\n\n"
            "---\n"
            "From: Jonas Weber (Customer Success) <jonas@ourcompany.example>\n"
            "To: Dr. Laura Pérez\n"
            "Cc: Maya Chen\n"
            "Date: 20 Aug 2026\n"
            "Subject: RE: Northwind — 40 licences confirmed\n\n"
            "Hi Laura, great to meet you. One thing I need to flag honestly: because Northwind "
            "handles patient data, your account has to pass our security review before we can "
            "connect SSO, and the review queue is currently about two weeks. We're aiming for early "
            "September for onboarding, but I can't guarantee a date until the review is done. I'll "
            "keep you posted weekly. Jonas\n\n"
            "This message and any attachments are confidential and intended only for the named "
            "recipient. If you received it in error, please notify the sender and delete it.\n\n"
            "---\n"
            "From: Dr. Laura Pérez\n"
            "To: Jonas Weber\n"
            "Date: 21 Aug 2026\n"
            "Subject: RE: RE: Northwind — 40 licences confirmed\n\n"
            "Jonas, this is the first I've heard of a security review. Maya was clear about "
            "September 1 and we planned our rota around it. Please treat this as urgent. Laura\n\n"
            "Dr. Laura Pérez | Clinical Operations Director | Northwind Clinics | 14 Harbour Road\n\n"
            "---\n"
            "From: Maya Chen (Sales)\n"
            "To: Jonas Weber\n"
            "Date: 21 Aug 2026\n"
            "Subject: Northwind — can we fast-track?\n\n"
            "Jonas, I honestly didn't know about the security review queue when I told Laura "
            "September 1. Is there any way to fast-track it? If not, can you manage her expectations "
            "and keep her updated? Maya\n\n"
            "---\n"
            "From: Jonas Weber\n"
            "To: Maya Chen\n"
            "Date: 21 Aug 2026\n"
            "Subject: RE: Northwind — can we fast-track?\n\n"
            "No fast-track, sorry: security owns the queue and accounts with patient data can't skip "
            "it. I'll send Laura weekly updates until it clears. Jonas\n\n"
            "---\n"
            "From: Jonas Weber\n"
            "To: Dr. Laura Pérez\n"
            "Date: 5 Sep 2026\n"
            "Subject: Security review complete\n\n"
            "Hi Laura, good news: the security review passed this morning. I'm scheduling the SSO "
            "connection with our engineers and will send you onboarding slots shortly. Jonas\n\n"
            "This message and any attachments are confidential and intended only for the named "
            "recipient. If you received it in error, please notify the sender and delete it.\n\n"
            "---\n"
            "From: Priya Nair (Finance) <priya@ourcompany.example>\n"
            "To: Jonas Weber, Maya Chen\n"
            "Date: 9 Sep 2026\n"
            "Subject: Northwind compensation request\n\n"
            "Jonas, Maya — re your request: a service credit of 15% of the first month's fee is "
            "approved for Northwind, to be applied on the next invoice. A full refund is not "
            "approved; the contract has no refund clause for onboarding delays. Priya\n\n"
            "---\n"
            "From: Dr. Laura Pérez\n"
            "To: Jonas Weber, Maya Chen\n"
            "Date: 11 Sep 2026\n"
            "Subject: This is unacceptable\n\n"
            "It is now 11 September. We still have no onboarding slots, my clinicians are running "
            "the autumn rota on paper, and nobody has told me why. I was promised September 1. I "
            "want a full refund and an explanation, or we will cancel. Laura\n"
            "---\n\n"
            "First list what was actually promised to Northwind, by whom and on which date. Then "
            "explain why Laura is upset, including anything our side did wrong. Then draft a reply "
            "to Laura of at most 120 words that offers only what has actually been approved and "
            "does not promise a new date."
        ),
        criteria=(
            Criterion("attributes_the_september_1_promise_to_maya",
                      _all(_has("maya"),
                           _matches(r"september 1\b|sept\.? 1\b|sep\.? 1\b|1 september|1st of september"
                                    r"|september 1st|2026-09-01|1 sep\b"))),
            Criterion("lists_the_weekly_update_promise",
                      _all(_has("jonas"), _has("weekly", "every week", "keep you posted", "posted weekly"))),
            Criterion("names_what_went_wrong",
                      _has("security review", "not told", "wasn't told", "was not told", "never told",
                           "not informed", "no update", "no onboarding slots", "didn't send",
                           "did not send", "weekly", "communicat")),
            Criterion("offers_the_approved_15_percent_credit", _matches(r"15\s*%|15 percent|fifteen percent")),
            Criterion(
                "does_not_offer_a_full_refund",
                lambda t: not re.search(
                    r"(we will|we'll|we can|we are happy to|happy to|we'd like to|we would like to"
                    r"|pleased to|able to|going to)\s+(offer|provide|issue|process|give|arrange)"
                    r"(\s+you)?\s+(a\s+|the\s+)?full refund", t, re.I),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion(
                "reply_does_not_promise_a_new_date",
                _reply_promises_no_new_date,
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=1200,
    ),
    BenchTask(
        task_id="std_chat_design_doc_retention",
        category="chat",
        capability="chat",
        prompt=(
            "I'm reviewing this design doc before sign-off. Here it is in full.\n\n"
            "# Event Pipeline v2 — Design\n\n"
            "## 1. Overview\n"
            "The event pipeline ingests product analytics events from the web and mobile apps, "
            "enriches them with account metadata, and makes them available to the analytics "
            "warehouse within 15 minutes. Version 2 replaces the nightly batch loader with a "
            "streaming consumer and moves raw storage from the legacy Hadoop cluster to object "
            "storage.\n\n"
            "## 2. Data retention\n"
            "Raw event logs are retained in the event store for 90 days and then deleted by the "
            "retention job. Aggregated tables in the warehouse contain no user identifiers and are "
            "retained indefinitely. The retention job runs daily at 02:00 UTC and deletes every raw "
            "event older than the retention period.\n\n"
            "## 3. Ingestion\n"
            "Clients send batches of up to 500 events over HTTPS. The ingestion service validates "
            "the schema, drops events with unknown types, and writes accepted events to the event "
            "store. Rejected events are counted per client version so that mobile releases with "
            "broken instrumentation can be detected within an hour.\n\n"
            "## 4. Enrichment\n"
            "A streaming consumer joins each event with the account record (plan, region, signup "
            "cohort). Enrichment never adds free-text fields. If the account service is unavailable, "
            "events are retried with exponential backoff for up to 6 hours and then written without "
            "enrichment, flagged as partial.\n\n"
            "## 5. Regional requirements\n"
            "For users whose account region is EU, raw event logs must be deleted no later than 30 "
            "days after collection, as required by the data processing agreement signed with our EU "
            "customers. This requirement overrides the general retention period in section 2 for EU "
            "users. The retention job applies the 30-day period to EU events and the 90-day period "
            "to all other events.\n\n"
            "## 6. Access control\n"
            "Only the analytics platform team can query raw events. Analysts query the warehouse "
            "aggregates. Every raw-event query is logged with the requesting user, the query text "
            "and a justification field, and the log is reviewed monthly. Service accounts used by "
            "the enrichment consumer can write raw events but cannot read them back. Access grants "
            "are reviewed every quarter by the data protection officer.\n\n"
            "## 7. Backups and disaster recovery\n"
            "The event store is backed up nightly to a separate region. Each nightly backup is a "
            "full snapshot of the event store and is kept for 60 days, after which it is deleted. "
            "Restores are tested quarterly by restoring the most recent backup into a staging "
            "environment.\n\n"
            "## 8. Monitoring\n"
            "We alert when ingestion lag exceeds 15 minutes, when the rejected-event rate for any "
            "client version exceeds 5%, and when the retention job fails to complete by 04:00 "
            "UTC.\n\n"
            "## 9. Rollout plan\n"
            "The streaming consumer runs in shadow mode for two weeks alongside the nightly batch "
            "loader, and the two outputs are compared daily. Once daily row counts match within 0.5% "
            "for seven consecutive days, the batch loader is switched off and the legacy Hadoop "
            "cluster is decommissioned on the same day.\n\n"
            "## 10. Cost estimate\n"
            "At the current volume of about 1.2 billion events per month, raw storage in object "
            "storage costs roughly $2,300 per month, the streaming consumer about $1,800 per month in "
            "compute, and the nightly backups about $900 per month. Decommissioning the Hadoop "
            "cluster saves about $6,500 per month, so the migration pays for itself within the first "
            "quarter.\n\n"
            "## 11. Open questions\n"
            "- Should partial (unenriched) events be backfilled once the account service recovers?\n"
            "- Do we need per-customer retention overrides beyond the EU rule?\n\n"
            "Explain step by step whether the retention rules for EU users' raw event logs are "
            "consistent across the whole document, and tell me the maximum number of days an EU "
            "user's raw event could currently still exist anywhere in our systems under this design."
        ),
        criteria=(
            Criterion("finds_the_backup_conflict",
                      _all(_has("backup", "snapshot"), _matches(r"\b60\b")), weight=2.0),
            Criterion("says_the_rules_conflict",
                      _has("inconsisten", "not consistent", "contradict", "conflict", "violat", "breach",
                           "not fully consistent", "gap")),
            Criterion("states_a_maximum_near_90_days",
                      _matches(r"(maximum|max|up to|as long as|as many as|could (still )?exist|remain"
                               r"|persist|survive|roughly|about|approximately|~)[^.\n]{0,60}\b(89|90)\b")),
            Criterion("reads_section_5_as_an_explicit_override", _has("overrid")),
            Criterion(
                "does_not_call_the_rules_consistent",
                _never_asserts("rules are consistent", "are fully consistent", "consistent across the document",
                               "consistent across the whole document", "consistent within the document",
                               "is consistent across", "are consistent across", "no inconsistency",
                               "no contradiction"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=1200,
    ),
    BenchTask(
        task_id="std_chat_asyncio_crash",
        category="chat",
        capability="chat",
        prompt=(
            "Our websocket gateway crashes a few times a day, only under load. Nothing in our code "
            "uses threads. Here is the relevant part of the service and the crash log.\n\n"
            "```python\n"
            "import asyncio\n"
            "import time\n\n"
            "class SessionRegistry:\n"
            "    def __init__(self):\n"
            '        self.sessions: dict[str, "Session"] = {}\n\n'
            '    async def register(self, session_id: str, session: "Session") -> None:\n'
            "        self.sessions[session_id] = session\n\n"
            "    async def unregister(self, session_id: str) -> None:\n"
            "        self.sessions.pop(session_id, None)\n\n"
            "    async def reap_idle(self, max_idle_s: float) -> int:\n"
            "        now = time.monotonic()\n"
            "        idle = []\n"
            "        for session_id, session in self.sessions.items():\n"
            "            if now - session.last_seen > max_idle_s:\n"
            "                await session.close()          # sends a close frame over the network\n"
            "                idle.append(session_id)\n"
            "        for session_id in idle:\n"
            "            self.sessions.pop(session_id, None)\n"
            "        return len(idle)\n\n"
            "async def reaper(registry: SessionRegistry) -> None:\n"
            "    while True:\n"
            "        await registry.reap_idle(max_idle_s=300)\n"
            "        await asyncio.sleep(30)\n"
            "```\n\n"
            "Crash log:\n\n"
            "```\n"
            "2026-09-12T14:03:22Z ERROR gateway.reaper task crashed\n"
            "Traceback (most recent call last):\n"
            '  File "/app/gateway/reaper.py", line 31, in reaper\n'
            "    await registry.reap_idle(max_idle_s=300)\n"
            '  File "/app/gateway/registry.py", line 18, in reap_idle\n'
            "    for session_id, session in self.sessions.items():\n"
            "RuntimeError: dictionary changed size during iteration\n"
            "2026-09-12T14:03:22Z WARN  gateway.reaper no reaper running; idle sessions will "
            "accumulate\n"
            "```\n\n"
            "First explain why this only happens under load even though there are no threads, then "
            "show the smallest correct fix to reap_idle."
        ),
        criteria=(
            Criterion("explains_that_await_yields_inside_the_loop",
                      _all(_has("await"),
                           _has("yield", "suspend", "gives up control", "hands control", "event loop",
                                "other coroutine", "other task", "another coroutine", "another task",
                                "scheduled", "control back", "pauses", "switch")),
                      weight=2.0),
            Criterion("names_the_concurrent_mutation",
                      _has("register", "unregister", "new connection", "new session", "sessions are added",
                           "added while", "removed while", "mutat", "modif")),
            Criterion("fix_stops_awaiting_while_iterating",
                      lambda t: _has("list(self.sessions.items())", "list(self.sessions", "tuple(self.sessions",
                                     "dict(self.sessions", "self.sessions.copy()", ".copy().items()",
                                     "asyncio.lock")(t) or _reap_idle_is_safe(t),
                      weight=2.0),
            Criterion(
                "does_not_blame_threads",
                _never_asserts("race between threads", "another thread", "multiple threads",
                               "threading.lock", "thread-safety issue", "thread safety issue"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=1000,
    ),
)


def complexity_of(task: BenchTask):
    """The routing complexity of a task, computed exactly as the router would.

    The router reads the capability, the LAST user message and whether tools
    are attached — never the system message. Tools are passed as
    function-shaped stubs because only their presence is a signal.
    """
    from trinity_router.complexity import compute_complexity

    tools = [{"type": "function", "function": {"name": n}} for n in task.tools_available] or None
    return compute_complexity(
        capability=task.capability,
        messages=[{"role": "system", "content": "policy"}, {"role": "user", "content": task.prompt}],
        tools=tools,
    )


def complexity_table() -> list[dict]:
    rows = []
    for task in STANDARD_TASKS:
        c = complexity_of(task)
        rows.append({
            "task_id": task.task_id, "category": task.category, "capability": task.capability,
            "score": c.score, "required_quality_class": c.required_quality_class,
            "chars": len(task.prompt), "tools": len(task.tools_available),
            **{k: v for k, v in c.signals.items() if v},
        })
    return rows


if __name__ == "__main__":
    for row in complexity_table():
        print(row)
