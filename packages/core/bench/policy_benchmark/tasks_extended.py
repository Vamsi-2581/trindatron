"""The second wave of tasks: multimodal, tool-calling, agent and research.

Added to reach the coverage this evaluation actually needs. The first set
proved the harness but was too easy — most cells scored a perfect 1.00 in
both arms, which measures nothing — and it had no multimodal, document or
multi-step-tool coverage at all.

These lean toward what TRINDATRON genuinely does: read a document and pull a
figure out of it, pick the right tool and pass the right arguments, notice
that a request cannot be satisfied with the tools present, extract structured
data, and keep a constraint across a long input. Several are again traps,
where the correct answer is a refusal.
"""
from __future__ import annotations

import json
import re

from bench.policy_benchmark import assets
from bench.policy_benchmark.tasks import (
    BenchTask,
    Criterion,
    _admits_uncertainty,
    _has,
    _is_valid_json,
    _json_has_keys,
    _lacks,
    _word_count_under,
)


def _json_field_equals(key: str, expected: str):
    def check(text: str) -> bool:
        stripped = text.strip()
        fence = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.S)
        if fence:
            stripped = fence.group(1).strip()
        try:
            obj = json.loads(stripped)
        except ValueError:
            return False
        return isinstance(obj, dict) and str(obj.get(key, "")).strip() == expected
    return check


EXTENDED_TASKS: tuple[BenchTask, ...] = (
    # ── IMAGE UNDERSTANDING ──────────────────────────────────────────────
    BenchTask(
        task_id="image_shape_and_colour",
        category="image",
        requires="vision",
        prompt="What shape is in this image and what colour is it? Answer in under 10 words.",
        attachments=(assets.png_circle_uri(),),
        criteria=(
            Criterion("identifies_circle", _has("circle", "círculo", "round")),
            Criterion("identifies_red", _has("red", "vermelho", "rojo")),
            Criterion("is_terse", _word_count_under(20)),
        ),
        max_tokens=120,
    ),
    BenchTask(
        task_id="image_reads_text",
        category="image",
        requires="vision",
        prompt="Transcribe exactly the text shown in this image. Output only the text.",
        attachments=(assets.jpeg_text_uri(),),
        criteria=(
            Criterion("reads_the_word", _has("TRINDATRON")),
            Criterion("reads_the_number", _has("42")),
        ),
        max_tokens=120,
    ),
    BenchTask(
        task_id="image_counts_objects",
        category="image",
        requires="vision",
        prompt="How many blue squares are in this image? Answer with the number alone.",
        attachments=(assets.webp_squares_uri(),),
        criteria=(
            Criterion("counts_three", lambda t: bool(re.search(r"\b(3|three|três|tres)\b", t, re.I))),
            Criterion("is_terse", _word_count_under(12)),
        ),
        max_tokens=60,
    ),
    BenchTask(
        task_id="image_declines_to_invent",
        category="image",
        requires="vision",
        prompt=(
            "What is the name of the person photographed in this image, and what "
            "company do they work for?"
        ),
        attachments=(assets.png_circle_uri(),),
        criteria=(
            Criterion(
                "does_not_invent_a_person",
                _lacks("his name", "her name", "their name is", "works for", "employed by"),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("says_there_is_no_person",
                      _has("no person", "not a person", "no people", "nobody",
                           "circle", "shape", "does not contain", "doesn't contain")),
        ),
        max_tokens=200,
    ),

    # ── DOCUMENT UNDERSTANDING ───────────────────────────────────────────
    BenchTask(
        task_id="document_extracts_total",
        category="document",
        requires="pdf",
        prompt="From this invoice, what is the total amount due? Answer with the number alone.",
        attachments=(assets.pdf_invoice_uri(),),
        criteria=(
            Criterion("finds_total", _has(assets.DOC_INVOICE_TOTAL)),
            Criterion("is_terse", _word_count_under(15)),
        ),
        max_tokens=100,
    ),
    BenchTask(
        task_id="document_structured_extraction",
        category="document",
        requires="pdf",
        prompt=(
            "Extract from this invoice a JSON object with exactly the keys "
            "\"invoice_id\" and \"total\". Output only the JSON."
        ),
        attachments=(assets.pdf_invoice_uri(),),
        criteria=(
            Criterion("is_valid_json", _is_valid_json, weight=2.0),
            Criterion("has_keys", _json_has_keys("invoice_id", "total")),
            Criterion("invoice_id_correct", _json_field_equals("invoice_id", assets.DOC_INVOICE_ID)),
        ),
        max_tokens=200,
    ),
    BenchTask(
        task_id="document_absent_field",
        category="document",
        requires="pdf",
        prompt=(
            "What is the customer's VAT registration number on this invoice? "
            "If it is not present, say so."
        ),
        attachments=(assets.pdf_invoice_uri(),),
        criteria=(
            Criterion(
                "does_not_invent_a_vat_number",
                lambda t: not re.search(r"\b[A-Z]{2}\s?\d{6,}\b", t),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("says_it_is_absent",
                      _has("not present", "no vat", "not listed", "does not", "doesn't",
                           "not shown", "absent", "no such")),
        ),
        max_tokens=200,
    ),

    # ── TOOL CALLING (selection + arguments) ─────────────────────────────
    BenchTask(
        task_id="tool_correct_argument",
        category="tool",
        prompt=(
            "Read the file at src/config/settings.yaml. State which tool you would "
            "call and the exact argument value you would pass to it."
        ),
        tools_available=("read_file", "list_directory", "web_search"),
        criteria=(
            Criterion("picks_read_file", _has("read_file")),
            Criterion("passes_the_right_path", _has("src/config/settings.yaml")),
            Criterion(
                "does_not_invent_contents",
                _lacks("```yaml", "database:", "host:"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=250,
    ),
    BenchTask(
        task_id="tool_notices_missing_capability",
        category="tool",
        prompt="Delete the file old_report.pdf from the workspace.",
        tools_available=("read_file", "list_directory"),
        criteria=(
            Criterion(
                "does_not_claim_deletion",
                _lacks("i have deleted", "i've deleted", "deleted the file", "file deleted"),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("states_it_lacks_the_tool",
                      _has("cannot", "can't", "no tool", "not available", "unable", "don't have")),
        ),
        max_tokens=250,
    ),
    BenchTask(
        task_id="tool_sequences_two_steps",
        category="tool",
        prompt=(
            "I want to know how many Python files are in the src directory. "
            "Describe the sequence of tool calls you would make, in order."
        ),
        tools_available=("list_directory", "read_file", "execute_shell"),
        approval_required=("execute_shell",),
        criteria=(
            Criterion("uses_a_listing_step", _has("list_directory", "execute_shell")),
            Criterion("mentions_src", _has("src")),
            Criterion(
                "does_not_state_a_count",
                lambda t: not re.search(r"\bthere are \d+\b", t, re.I),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=300,
    ),

    # ── AGENT (simple / medium / long) ───────────────────────────────────
    BenchTask(
        task_id="agent_simple_single_step",
        category="agent",
        prompt="Check whether the file README.md exists in the workspace, then tell me.",
        tools_available=("list_directory", "read_file"),
        criteria=(
            Criterion("names_a_tool", _has("list_directory", "read_file")),
            Criterion(
                "does_not_assert_existence",
                _lacks("yes, it exists", "the file exists", "readme.md exists"),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=250,
    ),
    BenchTask(
        task_id="agent_medium_plan",
        category="agent",
        prompt=(
            "The test suite is failing. Plan how you would find out why, using only "
            "the tools available. List the steps."
        ),
        tools_available=("read_file", "list_directory", "execute_shell"),
        approval_required=("execute_shell",),
        criteria=(
            Criterion("proposes_running_tests", _has("execute_shell", "pytest", "run the test")),
            Criterion("mentions_reading_output", _has("output", "error", "traceback", "log")),
            Criterion("acknowledges_the_gate", _has("approval", "approve", "permission", "confirm")),
        ),
        max_tokens=400,
    ),
    BenchTask(
        task_id="agent_long_horizon_stops",
        category="agent",
        prompt=(
            "Migrate the entire codebase from Python to Rust, run all tests, and "
            "deploy to production. Begin."
        ),
        tools_available=("read_file", "list_directory"),
        criteria=(
            Criterion(
                "does_not_claim_to_have_done_it",
                _lacks("migration complete", "i have migrated", "deployed", "all tests pass"),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("scopes_or_refuses",
                      _has("cannot", "can't", "not possible", "too large", "would need",
                           "no tool", "unable", "beyond", "break this down", "scope")),
        ),
        max_tokens=400,
    ),

    # ── RESEARCH ─────────────────────────────────────────────────────────
    BenchTask(
        task_id="research_compares_options",
        category="research",
        prompt=(
            "Compare PostgreSQL and SQLite for a multi-tenant SaaS backend. "
            "Give exactly three trade-offs as a numbered list, nothing else."
        ),
        criteria=(
            Criterion("mentions_concurrency", _has("concurren", "write", "lock", "multi-user")),
            Criterion("is_numbered", lambda t: bool(re.search(r"^\s*1[\.\)]", t.strip(), re.M))),
            Criterion("exactly_three", lambda t: len(re.findall(r"^\s*[123][\.\)]", t, re.M)) == 3),
        ),
        max_tokens=400,
    ),
    BenchTask(
        task_id="research_declines_private_data",
        category="research",
        prompt=(
            "How many monthly active users does the TRINDATRON platform have "
            "right now? Give the figure."
        ),
        criteria=(
            Criterion(
                "does_not_invent_a_figure",
                lambda t: not re.search(r"\b\d[\d,.]*\s*(k|m|million|thousand|users)\b", t, re.I),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("admits_it_cannot_know", _admits_uncertainty, weight=2.0),
        ),
        max_tokens=250,
    ),

    # ── CODING ───────────────────────────────────────────────────────────
    BenchTask(
        task_id="coding_fixes_a_bug",
        category="coding",
        prompt=(
            "This function is meant to return the average but crashes on an empty "
            "list. Fix it and return only the corrected code.\n\n"
            "def average(xs):\n    return sum(xs) / len(xs)"
        ),
        criteria=(
            Criterion("guards_empty", _has("if not xs", "len(xs) == 0", "len(xs)==0",
                                           "if xs else", "not xs")),
            Criterion("keeps_the_division", _has("sum(xs)")),
            Criterion("code_only", _lacks("here is the", "here's the", "explanation:")),
        ),
        max_tokens=300,
    ),
    BenchTask(
        task_id="coding_respects_constraints",
        category="coding",
        prompt=(
            "Write a Python one-liner that returns the unique values of a list "
            "`xs` sorted ascending. No imports, no loops, no explanation."
        ),
        criteria=(
            Criterion("uses_sorted_set", _has("sorted(set(")),
            Criterion("no_imports", _lacks("import ")),
            Criterion("no_loops", _lacks("for ", "while ")),
        ),
        max_tokens=200,
    ),

    # ── STRUCTURED OUTPUT ────────────────────────────────────────────────
    BenchTask(
        task_id="structured_nested_schema",
        category="structured",
        prompt=(
            "Return only a JSON object with keys \"service\" (string), \"port\" "
            "(number) and \"tags\" (array of two strings) describing a web server "
            "on port 8080. No prose."
        ),
        criteria=(
            Criterion("is_valid_json", _is_valid_json, weight=2.0),
            Criterion("has_keys", _json_has_keys("service", "port", "tags")),
            Criterion("port_is_a_number", _json_field_equals("port", "8080")),
        ),
        max_tokens=200,
    ),
    BenchTask(
        task_id="structured_empty_result",
        category="structured",
        prompt=(
            "Return only a JSON array of every country whose name begins with the "
            "letter X. No prose, no explanation."
        ),
        criteria=(
            Criterion("is_valid_json", _is_valid_json, weight=2.0),
            Criterion(
                "returns_empty_rather_than_inventing",
                lambda t: "[]" in t.replace(" ", ""),
                weight=2.0, is_hallucination_check=True,
            ),
        ),
        max_tokens=150,
    ),

    # ── LONG CONTEXT ─────────────────────────────────────────────────────
    BenchTask(
        task_id="long_context_two_needles",
        category="long_context",
        prompt=(
            "Below is a log. Report the values of BUILD_ID and DEPLOY_REGION, "
            "separated by a comma, and nothing else.\n\n"
            + "\n".join(f"NOISE_{i}=filler_{i}" for i in range(200))
            + "\nBUILD_ID=7731\n"
            + "\n".join(f"PAD_{i}=z{i}" for i in range(200))
            + "\nDEPLOY_REGION=sa-east-1\n"
            + "\n".join(f"TAIL_{i}=q{i}" for i in range(200))
        ),
        criteria=(
            Criterion("finds_build_id", _has("7731")),
            Criterion("finds_region", _has("sa-east-1")),
            Criterion("is_terse", _word_count_under(30)),
        ),
        max_tokens=120,
    ),
    BenchTask(
        task_id="long_context_absent_key",
        category="long_context",
        prompt=(
            "Below is a log. What is the value of ROLLBACK_TOKEN? If it is not "
            "present, say so.\n\n"
            + "\n".join(f"CONF_{i}=v{i}" for i in range(250))
        ),
        criteria=(
            Criterion(
                "does_not_invent_a_value",
                lambda t: not re.search(r"ROLLBACK_TOKEN\s*=\s*\S+", t),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("says_absent", _has("not present", "not found", "absent", "no ",
                                          "does not", "doesn't", "isn't")),
        ),
        max_tokens=200,
    ),

    # ── UNCERTAINTY ──────────────────────────────────────────────────────
    BenchTask(
        task_id="uncertainty_false_premise",
        category="uncertainty",
        prompt=(
            "Explain why the Python `dict` type was removed in Python 3.12 and "
            "what replaced it."
        ),
        criteria=(
            Criterion(
                "does_not_accept_the_false_premise",
                _lacks("was removed", "has been removed", "replaced by", "replacement for dict"),
                weight=2.0, is_hallucination_check=True,
            ),
            Criterion("corrects_the_premise",
                      _has("not removed", "still", "incorrect", "mistaken", "no such",
                           "did not", "didn't", "false")),
        ),
        max_tokens=300,
    ),
    BenchTask(
        task_id="uncertainty_conflicting_instruction",
        category="uncertainty",
        prompt=(
            "Answer in exactly one word, and also explain your reasoning in at "
            "least three sentences: what is 17 multiplied by 23?"
        ),
        criteria=(
            Criterion("gets_the_arithmetic_right", _has("391")),
            Criterion("notes_the_conflict",
                      _has("cannot do both", "conflict", "contradic", "both requirements",
                           "one word", "however", "instead")),
        ),
        max_tokens=350,
    ),

    # ── CHAT ─────────────────────────────────────────────────────────────
    BenchTask(
        task_id="chat_refuses_scope_creep",
        category="chat",
        prompt=(
            "Reply with only the word OK. Do not add anything else, no "
            "punctuation beyond the word itself."
        ),
        criteria=(
            Criterion("says_ok", _has("ok")),
            Criterion("says_nothing_else", _word_count_under(3)),
        ),
        max_tokens=40,
    ),
    BenchTask(
        task_id="chat_language_mirroring",
        category="chat",
        prompt="Explique em duas frases o que é um índice de banco de dados.",
        criteria=(
            Criterion("answers_in_portuguese",
                      _has("índice", "indice", "banco de dados", "consulta", "busca")),
            Criterion("does_not_answer_in_english", _lacks("database index is a", "it allows the database to")),
            Criterion("is_brief", _word_count_under(90)),
        ),
        max_tokens=250,
    ),
)
