"""Etapa 5C — unit tests for LoadClassifier.

Proves: chat textual simples -> LIGHT; Agent -> HEAVY; an existing
intermediate task (FILES) -> MEDIUM; every existing Capability maps to a
documented class; unknown/ambiguous inputs round up conservatively;
classification is deterministic (pure function of its inputs); no global
mutable state is read or written; and classification is safe to call
concurrently from many threads at once (no lock needed, because there is
nothing to lock).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from trinity_router import TaskType

from src.capability import Capability, get_task_type
from src.services.load_classification import LoadClass, LoadClassifier, load_classifier


# ── 1. Chat textual simples -> LIGHT ────────────────────────────────────────

def test_chat_capability_is_light():
    assert LoadClassifier().classify(Capability.CHAT) == LoadClass.LIGHT


def test_chat_is_light_even_with_forced_task_type_chat():
    # Mirrors the /chat route's actual call: forced_task_type=TaskType.CHAT
    # regardless of what capability the raw content resembles.
    assert LoadClassifier().classify(Capability.CODE, forced_task_type=TaskType.CHAT) == LoadClass.LIGHT


# ── 2. Agent -> HEAVY ────────────────────────────────────────────────────────

def test_agent_capability_is_heavy():
    assert LoadClassifier().classify(Capability.AGENT) == LoadClass.HEAVY


def test_agent_is_heavy_even_when_content_looks_like_plain_chat():
    # Mirrors the /agent route's actual call: forced_task_type=TaskType.AGENT
    # even if classify(body.content) guessed Capability.CHAT from plain text
    # like "hello" — the endpoint forces the full ReAct loop regardless.
    assert LoadClassifier().classify(Capability.CHAT, forced_task_type=TaskType.AGENT) == LoadClass.HEAVY


# ── 3. An existing intermediate task -> MEDIUM ──────────────────────────────

def test_files_capability_is_medium():
    assert LoadClassifier().classify(Capability.FILES) == LoadClass.MEDIUM


# ── 4. Every existing Capability maps to a documented class ────────────────

EXPECTED_LOAD_CLASS = {
    Capability.CHAT: LoadClass.LIGHT,
    Capability.IMAGE: LoadClass.LIGHT,
    Capability.VIDEO: LoadClass.LIGHT,
    Capability.AUDIO: LoadClass.LIGHT,
    Capability.FILES: LoadClass.MEDIUM,
    Capability.RESEARCH: LoadClass.HEAVY,
    Capability.CODE: LoadClass.HEAVY,
    Capability.WORK: LoadClass.HEAVY,
    Capability.AUTOMATION: LoadClass.HEAVY,
    Capability.AGENT: LoadClass.HEAVY,
    # Minutes of wall time, a model call per slide, a rendering container.
    Capability.PRESENTATION: LoadClass.HEAVY,
}


def test_every_capability_has_a_documented_load_class():
    # Fails loudly if a new Capability is ever added without updating this
    # test (and, presumably, load_classification.py's own mapping).
    assert set(EXPECTED_LOAD_CLASS) == set(Capability)


def test_load_class_mapping_matches_documented_rationale():
    clf = LoadClassifier()
    for capability, expected in EXPECTED_LOAD_CLASS.items():
        assert clf.classify(capability) == expected, f"{capability} expected {expected}"


def test_image_video_audio_are_light_not_heavy():
    """Explicit regression guard for the product instruction: do not assume
    every non-textual capability is HEAVY. IMAGE/VIDEO/AUDIO are
    intercepted before any executor runs (immediate "coming soon" reply) —
    the lightest possible request in the system."""
    clf = LoadClassifier()
    for capability in (Capability.IMAGE, Capability.VIDEO, Capability.AUDIO):
        assert clf.classify(capability) == LoadClass.LIGHT


def test_work_browser_automation_is_heavy():
    assert LoadClassifier().classify(Capability.WORK) == LoadClass.HEAVY


# ── 5. Unknown/ambiguous inputs round up conservatively ─────────────────────

def test_forced_agent_task_type_defaults_unmapped_capability_to_heavy():
    # RESEARCH/CODE (and any future capability not explicitly listed in the
    # AGENT-bucket override dict) fall through to the conservative HEAVY
    # default, never to MEDIUM or LIGHT.
    clf = LoadClassifier()
    for capability in (Capability.RESEARCH, Capability.CODE, Capability.AUTOMATION):
        assert clf.classify(capability, forced_task_type=TaskType.AGENT) == LoadClass.HEAVY


def test_omitted_forced_task_type_derives_the_same_result_as_get_task_type():
    """Confirms the /message route's usage pattern (task_type IS
    get_task_type(capability) there) is equivalent to passing it
    explicitly — no hidden divergence between the two call styles."""
    clf = LoadClassifier()
    for capability in Capability:
        derived_task_type = get_task_type(capability)
        assert clf.classify(capability) == clf.classify(capability, forced_task_type=derived_task_type)


# ── 6. Deterministic ─────────────────────────────────────────────────────────

def test_classification_is_deterministic_across_many_repeated_calls():
    clf = LoadClassifier()
    for capability in Capability:
        results = {clf.classify(capability) for _ in range(200)}
        assert len(results) == 1, f"{capability} produced varying results: {results}"


def test_classification_is_deterministic_across_fresh_classifier_instances():
    for capability, expected in EXPECTED_LOAD_CLASS.items():
        assert LoadClassifier().classify(capability) == expected
        assert LoadClassifier().classify(capability) == expected


# ── 7. No external calls / purely local / fast ──────────────────────────────

def test_classification_is_fast_and_purely_local():
    """10,000 calls well under a second is strong indirect evidence there is
    no network I/O, no LLM call, and no blocking operation on the hot path
    — a real HTTP/LLM call would make this take orders of magnitude longer."""
    clf = LoadClassifier()
    capabilities = list(Capability)
    start = time.perf_counter()
    for i in range(10_000):
        clf.classify(capabilities[i % len(capabilities)])
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"10,000 classify() calls took {elapsed:.3f}s — too slow for a purely local operation"


# ── 8. No state mutation ────────────────────────────────────────────────────

def test_classify_does_not_mutate_any_module_state():
    from src.services import load_classification as mod

    chat_class_before = mod._CHAT_TASK_LOAD_CLASS
    agent_map_before = dict(mod._AGENT_CAPABILITY_LOAD_CLASS)

    clf = LoadClassifier()
    for capability in Capability:
        clf.classify(capability)
        clf.classify(capability, forced_task_type=TaskType.CHAT)
        clf.classify(capability, forced_task_type=TaskType.AGENT)

    assert mod._CHAT_TASK_LOAD_CLASS == chat_class_before
    assert mod._AGENT_CAPABILITY_LOAD_CLASS == agent_map_before


def test_multiple_classifier_instances_are_independent_and_stateless():
    clf_a = LoadClassifier()
    clf_b = LoadClassifier()
    assert clf_a.classify(Capability.FILES) == clf_b.classify(Capability.FILES)
    # Using one instance repeatedly cannot affect what the other returns —
    # there is no instance attribute either could have mutated.
    for _ in range(50):
        clf_a.classify(Capability.AGENT)
    assert clf_b.classify(Capability.FILES) == LoadClass.MEDIUM


def test_the_process_wide_singleton_behaves_identically_to_a_fresh_instance():
    for capability, expected in EXPECTED_LOAD_CLASS.items():
        assert load_classifier.classify(capability) == expected


# ── 9. Concurrency-safe ──────────────────────────────────────────────────────

def test_classification_is_safe_under_concurrent_calls_from_many_threads():
    """Pure function, no shared mutable state read or written by classify()
    — hammer it from many threads at once and confirm every result is
    exactly the expected one, with no cross-talk between threads."""
    clf = LoadClassifier()
    capabilities = list(Capability) * 200  # 2000 calls spread across threads

    def classify_and_check(capability: Capability) -> bool:
        return clf.classify(capability) == EXPECTED_LOAD_CLASS[capability]

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(classify_and_check, capabilities))

    assert all(results), "at least one concurrent classify() call returned an unexpected result"
