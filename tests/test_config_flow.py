"""Tests for the config and options flow schemas.

    python tests/test_config_flow.py

Requires voluptuous (`pip install voluptuous`), which the config flow itself
depends on. Everything else is stubbed as usual.

These exist because a field was once added to the schema in a way that was
syntactically valid but semantically wrong — it landed as vol.Optional's
positional `msg` argument instead of as a key, so the option silently never
rendered in Home Assistant. Nothing caught it: the file parsed, imports were
clean, and no test touched the config flow. Asserting on the built schema's
actual keys is what closes that gap.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stub_hass  # noqa: E402,F401  (installs the fake homeassistant modules)

try:
    import voluptuous as vol
except ImportError:  # pragma: no cover
    print("SKIP: voluptuous not installed (pip install voluptuous)")
    raise SystemExit(0)

import types  # noqa: E402


def _install_config_flow_stubs() -> None:
    """The extra HA surface only the config flow touches."""
    config_entries = sys.modules["homeassistant.config_entries"]

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

    class OptionsFlow:
        pass

    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow

    helpers = sys.modules["homeassistant.helpers"]
    selector_module = types.ModuleType("homeassistant.helpers.selector")

    def fake_selector(config):
        """Permissive validator that remembers the selector config it was built from.

        Returning the raw config instead would make voluptuous treat it as a
        nested schema; a callable is validated as a validator, which is what
        the real selector behaves like.
        """

        def validator(value):
            return value

        validator.selector_config = config
        return validator

    selector_module.selector = fake_selector
    sys.modules["homeassistant.helpers.selector"] = selector_module
    helpers.selector = selector_module


_install_config_flow_stubs()

const = stub_hass.load("const")
config_flow = stub_hass.load("config_flow")


def schema_keys(schema: vol.Schema) -> list[str]:
    """The option names a built schema will actually render."""
    return [str(marker.schema) for marker in schema.schema]


# ── schema shape ──────────────────────────────────────────────────────────
def test_main_schema_exposes_every_option():
    keys = schema_keys(config_flow._build_main_schema())
    assert keys == [
        const.CONF_PERSONS,
        const.CONF_SCAN_INTERVAL,
        const.CONF_EVENT_RADIUS_M,
        const.CONF_TRACK_HUBS,
    ], keys


def test_track_hubs_is_a_real_key_not_a_positional_argument():
    """The exact regression: the field must be a schema *key*.

    Passing it as vol.Optional's second positional argument (`msg`) is valid
    Python and valid voluptuous — the schema simply comes out one field short
    and the option never appears in the UI.
    """
    schema = config_flow._build_main_schema()
    assert const.CONF_TRACK_HUBS in schema_keys(schema)

    for marker in schema.schema:
        # A stray positional would surface here instead of as a key.
        assert marker.msg is None, f"{marker.schema} has an unexpected msg: {marker.msg}"


def selector_config_for(schema: vol.Schema, key: str) -> dict:
    for marker, validator in schema.schema.items():
        if str(marker.schema) == key:
            return getattr(validator, "selector_config", {})
    raise AssertionError(f"{key} not in schema")


def test_track_hubs_renders_as_a_boolean_toggle():
    """A number or text selector here would render, but not as a toggle."""
    config = selector_config_for(config_flow._build_main_schema(), const.CONF_TRACK_HUBS)
    assert "boolean" in config, config


def test_track_hubs_defaults_to_off():
    """Hub tracking must be opt-in; it calls a third-party API."""
    schema = config_flow._build_main_schema()
    marker = next(
        m for m in schema.schema if str(m.schema) == const.CONF_TRACK_HUBS
    )
    assert marker.default() is False


def test_track_hubs_default_reflects_the_saved_value():
    """Reopening options must show the toggle as the user last left it."""
    schema = config_flow._build_main_schema(default_track_hubs=True)
    marker = next(
        m for m in schema.schema if str(m.schema) == const.CONF_TRACK_HUBS
    )
    assert marker.default() is True


def test_schema_validates_a_submission_including_the_toggle():
    schema = config_flow._build_main_schema()
    result = schema(
        {
            const.CONF_PERSONS: ["person.steve"],
            const.CONF_SCAN_INTERVAL: 15,
            const.CONF_EVENT_RADIUS_M: 300,
            const.CONF_TRACK_HUBS: True,
        }
    )
    assert result[const.CONF_TRACK_HUBS] is True


def test_omitted_toggle_falls_back_to_the_default():
    schema = config_flow._build_main_schema()
    result = schema(
        {
            const.CONF_PERSONS: ["person.steve"],
            const.CONF_SCAN_INTERVAL: 15,
            const.CONF_EVENT_RADIUS_M: 300,
        }
    )
    assert result[const.CONF_TRACK_HUBS] is False


# ── the submit handlers read what the schema produces ─────────────────────
def test_submit_handlers_read_every_option():
    """Guards the second half of the same bug.

    The corruption also turned `user_input.get(CONF_EVENT_RADIUS_M, DEFAULT)`
    into a three-argument call, which raises TypeError on submit. Reading the
    source is crude, but this is a flow that cannot be driven without far more
    of Home Assistant stubbed than the bug is worth.
    """
    import ast
    import inspect

    source = inspect.getsource(config_flow)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            assert len(node.args) <= 2, (
                f"dict.get() with {len(node.args)} positional args on line "
                f"{node.lineno} — a key was almost certainly inserted by mistake"
            )

    # And the toggle must actually be read out of the submitted input.
    assert f"user_input.get({const.CONF_TRACK_HUBS!r}" in source or (
        "user_input.get(CONF_TRACK_HUBS" in source
    ), "track_hubs is never read from user_input"


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = []
    for name, test in tests:
        try:
            test()
        except AssertionError as err:
            failures.append(name)
            print(f"FAIL  {name}\n      {err}")
        except Exception as err:  # noqa: BLE001 - a crash is a failure too
            failures.append(name)
            print("ERROR " + name)
            print("      " + type(err).__name__ + ": " + str(err))
        else:
            print(f"pass  {name}")

    print()
    if failures:
        print(f"{len(failures)} of {len(tests)} failed")
        return 1
    print(f"all {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
