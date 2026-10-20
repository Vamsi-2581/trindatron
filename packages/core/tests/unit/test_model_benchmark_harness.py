"""The model-comparison harness: blocked models, modalities, and assets.

A benchmark that silently mis-handles a missing credential, or scores a
text-only model zero on an image task, produces numbers that look like model
findings and are not. These pin the three behaviours most likely to do that.

No model is called here.
"""
from __future__ import annotations

import base64
import json

import pytest

from bench.policy_benchmark import assets
from bench.policy_benchmark.runner import (
    ModelSpec,
    _anthropic_user_content,
    _openai_user_content,
    run,
)
from bench.policy_benchmark.tasks import TASKS


# ── A missing credential is BLOCKED, never a zero ────────────────────────

def test_a_model_without_a_credential_is_blocked_and_never_called(monkeypatch):
    """The mission's rule: do not invent a result. A model with no key must
    produce a stated blockage, not a row of failures that averages into the
    comparison as if it had tried and done badly."""
    called = []

    def _explode(*a, **k):
        called.append(1)
        raise AssertionError("a blocked model must never be called")

    monkeypatch.setattr("bench.policy_benchmark.runner.call_model", _explode)
    monkeypatch.setattr("bench.policy_benchmark.runner._read_env", lambda *a, **k: {})

    spec = ModelSpec(key="ghost", endpoint="https://example.invalid",
                     model_id="ghost-1", api_key_env="GHOST_API_KEY",
                     usd_per_1m_input=1.0, usd_per_1m_output=1.0)
    outcomes = run([spec], repeats=1, out_dir="bench/results")

    assert called == []
    assert outcomes and all(o.arm == "blocked" for o in outcomes)
    assert all("GHOST_API_KEY" in o.error for o in outcomes)
    assert all(o.quality == 0.0 for o in outcomes)


def test_blocked_rows_are_excluded_from_quality_by_their_arm():
    """Blocked rows carry arm="blocked", which the summariser filters on —
    so they cannot be averaged into control or policy."""
    from bench.policy_benchmark.runner import TaskOutcome

    o = TaskOutcome(task_id="t", category="chat", arm="blocked",
                    model_key="ghost", repeat=0, error="missing KEY")
    assert o.arm not in ("control", "policy")


# ── An unsupported modality is a capability fact, not a failure ───────────

def test_a_text_only_model_skips_vision_tasks_rather_than_failing(monkeypatch):
    called = []

    def _spy(spec, system, user, max_tokens, env, attachments=()):
        called.append(attachments)
        from bench.policy_benchmark.runner import CallResult
        return CallResult("ok", 1, 1, 0.01)

    monkeypatch.setattr("bench.policy_benchmark.runner.call_model", _spy)
    monkeypatch.setattr("bench.policy_benchmark.runner._read_env",
                        lambda *a, **k: {"T_API_KEY": "present"})

    spec = ModelSpec(key="textonly", endpoint="https://example.invalid",
                     model_id="t", api_key_env="T_API_KEY",
                     usd_per_1m_input=1.0, usd_per_1m_output=1.0,
                     modalities=frozenset({"text"}))
    outcomes = run([spec], repeats=1, out_dir="bench/results")

    # No image or pdf task was ever sent.
    assert all(a == () for a in called)
    unsupported = [o for o in outcomes if o.unsupported]
    assert unsupported, "vision/pdf tasks should have been marked unsupported"
    assert {o.category for o in unsupported} >= {"image", "document"}


def test_unsupported_rows_carry_no_quality_to_average():
    from bench.policy_benchmark.runner import TaskOutcome

    o = TaskOutcome(task_id="t", category="image", arm="control",
                    model_key="m", repeat=0, unsupported=True)
    assert o.unsupported is True and o.quality == 0.0


# ── Multimodal payload shapes ────────────────────────────────────────────

def test_text_only_calls_send_a_plain_string():
    """Some providers are stricter about block form; there is no reason to use
    it when there is nothing to attach."""
    assert _openai_user_content("hello", ()) == "hello"
    assert _anthropic_user_content("hello", ()) == "hello"


def test_images_become_image_blocks_and_pdfs_become_file_blocks():
    content = _openai_user_content("q", (assets.png_circle_uri(), assets.pdf_invoice_uri()))
    kinds = [b["type"] for b in content]
    assert kinds == ["text", "image_url", "file"]


def test_anthropic_uses_source_blocks_with_the_right_media_type():
    content = _anthropic_user_content("q", (assets.pdf_invoice_uri(),))
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert content[-1]["type"] == "text"


# ── Generated assets ─────────────────────────────────────────────────────

@pytest.mark.parametrize("uri_fn,prefix", [
    (assets.png_circle_uri, "data:image/png;base64,"),
    (assets.jpeg_text_uri, "data:image/jpeg;base64,"),
    (assets.webp_squares_uri, "data:image/webp;base64,"),
    (assets.pdf_invoice_uri, "data:application/pdf;base64,"),
])
def test_assets_are_valid_data_uris(uri_fn, prefix):
    uri = uri_fn()
    assert uri.startswith(prefix)
    base64.b64decode(uri.split(",", 1)[1], validate=True)


def test_the_generated_pdf_is_structurally_valid():
    """Hand-built, so it is worth asserting it is a PDF rather than trusting
    the generator — otherwise a document task would be testing my byte
    assembly rather than the model's reading."""
    raw = assets.pdf_invoice_bytes()
    assert raw.startswith(b"%PDF-")
    assert b"%%EOF" in raw
    assert b"/Type /Catalog" in raw and b"startxref" in raw
    assert assets.DOC_INVOICE_TOTAL.encode() in raw


def test_expected_answers_live_beside_the_drawing_code():
    """The checker asserts these constants; the asset paints them. Keeping
    both in one module is what stops them diverging."""
    assert assets.SHAPE_COLOR == "red" and assets.SHAPE_KIND == "circle"
    assert assets.DOC_INVOICE_ID in assets.pdf_invoice_bytes().decode("latin-1")


# ── Coverage the mission asked for ───────────────────────────────────────

def test_the_task_set_meets_the_required_size_and_breadth():
    assert len(TASKS) >= 30
    categories = {t.category for t in TASKS}
    for required in ("chat", "research", "coding", "structured", "tool",
                     "agent", "long_context", "uncertainty", "image", "document"):
        assert required in categories, f"missing category: {required}"


def test_multimodal_tasks_declare_their_modality():
    for task in TASKS:
        if task.attachments:
            assert task.requires in ("vision", "pdf")
        if task.requires in ("vision", "pdf"):
            assert task.attachments, f"{task.task_id} declares a modality but attaches nothing"


def test_saved_reports_contain_metrics_not_transcripts(tmp_path):
    """Item 21: keep metrics, not model responses."""
    from bench.policy_benchmark.runner import TaskOutcome, save_report

    o = TaskOutcome(task_id="t", category="chat", arm="policy", model_key="m",
                    repeat=0, quality=1.0)
    o.response = "a model response that must not be persisted"
    path = tmp_path / "r.json"
    save_report([o], str(path))
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert "response" not in rows[0]
    assert "quality" in rows[0] and "cost_usd" in rows[0]
