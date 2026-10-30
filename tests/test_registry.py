import pytest

from tools.registry import ToolContext, ToolRegistry, ToolSpec


def _spec(name, handler, profiles=("lead", "subagent", "teammate")):
    return ToolSpec(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        profiles=profiles,
    )


def test_register_dispatch_and_exception_wrapping():
    registry = ToolRegistry()
    registry.register(_spec("echo", lambda args, ctx: args["value"]))
    ctx = ToolContext()
    assert registry.dispatch("echo", {"value": "hi"}, ctx) == "hi"
    assert "unknown tool" in registry.dispatch("nope", {}, ctx)

    registry.register(_spec("boom", lambda args, ctx: (_ for _ in ()).throw(ValueError("bad"))))
    assert "ValueError: bad" in registry.dispatch("boom", {}, ctx)


def test_duplicate_registration_and_replace():
    registry = ToolRegistry()
    registry.register(_spec("x", lambda args, ctx: "one"))
    with pytest.raises(ValueError):
        registry.register(_spec("x", lambda args, ctx: "two"))
    registry.register(_spec("x", lambda args, ctx: "two"), replace=True)
    assert registry.dispatch("x", {}, ToolContext()) == "two"


def test_profile_subset_controls_visibility():
    registry = ToolRegistry()
    registry.register(_spec("base", lambda args, ctx: "b"))
    registry.register(_spec("lead_only", lambda args, ctx: "l", profiles=("lead",)))

    lead_names = registry.subset("lead").names()
    sub_names = registry.subset("subagent").names()
    assert "lead_only" in lead_names
    assert "lead_only" not in sub_names
    assert registry.subset("subagent").dispatch("lead_only", {}, ToolContext()).startswith("Error:")
