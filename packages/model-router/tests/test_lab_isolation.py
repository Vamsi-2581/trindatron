"""Lab candidates must not be reachable by production routing.

The whole safety argument for evaluating a new model inside the real
codebase is that it cannot be selected. These tests are that argument,
written down: if any of them fails, a model under evaluation has become a
model in production, and that is a spending decision nobody made.
"""
from __future__ import annotations

import pytest

from trinity_router.lab import LAB_CATALOG, lab_keys, lab_model
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.selector import select_provider_chain
from trinity_router import TaskType


def test_no_lab_model_is_in_the_production_catalog():
    """Structural isolation: routing selects from PROVIDER_CATALOG, so a key
    absent from it cannot be chosen by any present or future code path."""
    overlap = set(LAB_CATALOG) & set(PROVIDER_CATALOG)
    assert not overlap, f"lab models leaked into production catalog: {overlap}"


def test_no_lab_model_id_is_in_the_production_catalog():
    """Also checked by model_id, not just key — a rename must not be enough
    to smuggle one in."""
    lab_ids = {m.model_id for m in LAB_CATALOG.values()}
    prod_ids = {m.model_id for m in PROVIDER_CATALOG.values()}
    assert not (lab_ids & prod_ids)


@pytest.mark.parametrize("plan", ["free", "start", "pro", "max", "team"])
@pytest.mark.parametrize("task", [TaskType.CHAT, TaskType.AGENT])
def test_no_selector_chain_offers_a_lab_model(plan, task):
    """The decisive check: every plan, every task type, no lab candidate."""
    chain = select_provider_chain(task, plan)
    for key in lab_keys():
        assert key not in chain, f"{key} appears in the {plan}/{task.value} chain"


def test_lab_module_is_not_imported_by_routing_code():
    """A dependency from router/selector onto this module would be the first
    step toward the isolation quietly ending."""
    import inspect

    from trinity_router import router, selector

    for module in (router, selector):
        source = inspect.getsource(module)
        assert "lab" not in [
            line.split()[1].split(".")[-1]
            for line in source.splitlines()
            if line.strip().startswith("from trinity_router") and len(line.split()) > 1
        ], f"{module.__name__} imports the lab registry"


# ── The facts the benchmark depends on ───────────────────────────────────

def test_lab_records_the_current_deepseek_model_id():
    """The provider renamed this: "deepseek-flash" is current,
    "deepseek-v4-flash" is a legacy alias. Hardcoding the old string is
    exactly the mistake the mission warned about."""
    assert lab_model("deepseek-flash").model_id == "deepseek/deepseek-flash"


def test_lab_records_glm_at_the_official_price():
    """$0.15/$0.50 from docs.z.ai, which litellm's zai/glm-5.3-flash entry
    independently agrees with."""
    glm = lab_model("glm-5.3-flash")
    assert glm.cost_per_1m_input_usd == pytest.approx(0.15)
    assert glm.cost_per_1m_output_usd == pytest.approx(0.50)


def test_deepseek_is_recorded_at_peak_not_off_peak():
    """Recording the discount as the headline price would make the cost model
    wrong for half the working week."""
    ds = lab_model("deepseek-flash")
    assert ds.cost_per_1m_input_usd == pytest.approx(0.30)
    assert ds.offpeak_multiplier == pytest.approx(0.5)
    assert "off-peak" in ds.offpeak_note.lower()


def test_every_lab_model_records_its_weights_licence():
    """Nothing gets self-hosted on an assumption. A permissive API does not
    imply permissive weights."""
    for model in LAB_CATALOG.values():
        assert model.weights_license != "unknown"
        assert isinstance(model.commercial_use, bool)


def test_credentials_are_referenced_by_env_name_only():
    """No key material in the registry, ever."""
    for model in LAB_CATALOG.values():
        if model.provider.startswith("local"):
            assert model.api_key_env == ""  # a loopback runtime has no credential at all (see below)
        else:
            assert model.api_key_env.endswith("_API_KEY")
        assert len(model.api_key_env) < 40
        for value in (model.model_id, model.api_base, model.api_key_env):
            assert "sk-" not in value


def test_unknown_lab_model_raises():
    with pytest.raises(ValueError):
        lab_model("glm-9000")


def test_local_lab_models_point_at_this_machine_and_need_no_credential():
    """A local candidate that could be pointed at a remote host would be an
    unpriced external provider in disguise."""
    from urllib.parse import urlsplit

    local = [m for m in LAB_CATALOG.values() if m.provider.startswith("local")]
    assert local, "the local llama.cpp candidate is recorded"
    for m in local:
        assert urlsplit(m.api_base).hostname in ("127.0.0.1", "localhost", "::1")
        assert m.api_key_env == ""
        assert not m.supports_tools and not m.supports_structured_output
