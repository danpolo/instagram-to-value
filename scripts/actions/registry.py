#!/usr/bin/env python3
"""Action registry framework (design spec Component 2). One module per
action type lives alongside this file; each declares the contract documented
in the Phase 4a plan's Task 2 (TYPE, RISK, SCHEMA, target_path, describe,
preview, collides, install) and calls register(sys.modules[__name__]) at
import time. load_all_handlers() imports every known handler module so
REGISTRY is fully populated -- interpret.py and install_artifact.py both call
it once at startup. generate_prompt_catalogue() builds the agent's action-type
menu straight from REGISTRY, so adding a handler automatically teaches the
classifier it exists; no prompt is ever hand-edited to list a new type."""
import importlib
import re

VALID_RISKS = ("inert", "config", "exec")
REGISTRY = {}

# Extended in Phase 4b (Session 4) as new handlers are added -- design spec's
# "pilot loop" turns logs/unsupported_actions.jsonl into the priority order.
HANDLER_MODULE_NAMES = [
    "package_install", "shell_snippet", "git_repo", "calendar_event",
    "rule", "skill", "reference", "note", "discard", "unsupported",
]

REQUIRED_MEMBERS = ("TYPE", "RISK", "SCHEMA", "target_path", "describe", "preview", "collides", "install")


def register(handler, registry_dict=None):
    """Called by each handler module at import time:
    register(sys.modules[__name__]) at the bottom of the file. Validates the
    module declares the full contract before adding it -- a handler missing a
    required member fails loudly at import time, not at first use."""
    target_dict = REGISTRY if registry_dict is None else registry_dict
    for attr in REQUIRED_MEMBERS:
        if not hasattr(handler, attr):
            name = getattr(handler, "__name__", repr(handler))
            raise AttributeError(f"action handler {name} missing required member {attr!r}")
    if handler.RISK not in VALID_RISKS:
        name = getattr(handler, "__name__", repr(handler))
        raise ValueError(f"action handler {name} has invalid RISK {handler.RISK!r}, must be one of {VALID_RISKS}")
    target_dict[handler.TYPE] = handler


def load_all_handlers():
    """Import every handler module in HANDLER_MODULE_NAMES so each one's
    register() call at import time populates the real REGISTRY. Assumes
    scripts/actions/ is already on sys.path (every caller inserts it, matching
    this repo's flat-sibling-import convention -- see interpret.py)."""
    for name in HANDLER_MODULE_NAMES:
        importlib.import_module(name)


def get_handler(action_type, registry_dict=None):
    return (REGISTRY if registry_dict is None else registry_dict).get(action_type)


def _check_field_type(value, expected_type):
    if expected_type is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected_type)


def validate_payload(action_type, payload, registry_dict=None):
    """Pure: (ok, error). Checks the handler's SCHEMA against payload.
    Unknown fields are allowed (forward-compatible); a missing required
    field, a wrong type, or a value outside a declared enum are not."""
    handler = get_handler(action_type, registry_dict)
    if handler is None:
        return False, f"unknown action type {action_type!r}"
    schema = handler.SCHEMA
    for field in schema.get("required", []):
        if field not in payload:
            return False, f"{action_type}: missing required payload field {field!r}"
    for field, expected_type in schema.get("types", {}).items():
        if field in payload and not _check_field_type(payload[field], expected_type):
            return False, f"{action_type}: payload field {field!r} must be {expected_type.__name__}"
    for field, allowed_values in schema.get("enum", {}).items():
        if field in payload and payload[field] not in allowed_values:
            return False, f"{action_type}: payload field {field!r} must be one of {allowed_values}"
    for field, pattern in schema.get("patterns", {}).items():
        if field in payload and not re.match(pattern, str(payload[field] or "")):
            return False, f"{action_type}: payload field {field!r} does not match the required format {pattern!r}"
    return True, None


def validate_action(action, registry_dict=None):
    """Pure: (ok, error) for one action dict {type, confidence, payload}."""
    if "type" not in action:
        return False, "action missing 'type'"
    action_type = action["type"]
    if "confidence" not in action:
        return False, f"{action_type}: action missing 'confidence'"
    confidence = action["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not (0.0 <= confidence <= 1.0):
        return False, f"{action_type}: confidence must be a number in [0, 1]"
    if "payload" not in action or not isinstance(action["payload"], dict):
        return False, f"{action_type}: action missing dict 'payload'"
    return validate_payload(action_type, action["payload"], registry_dict)


def validate_actions(actions, registry_dict=None):
    """Pure: (ok, errors) for a whole action list -- design spec's 'Schema
    violation in the returned action list -> same path as an unparseable
    response. Nothing partially-valid is staged.'"""
    errors = []
    for action in actions:
        ok, error = validate_action(action, registry_dict)
        if not ok:
            errors.append(error)
    return (len(errors) == 0), errors


def generate_prompt_catalogue(registry_dict=None):
    """Builds the agent prompt's action-type menu from the registry -- one
    line per type: risk tier, required payload fields, and the handler
    module's one-line docstring."""
    target_dict = REGISTRY if registry_dict is None else registry_dict
    lines = []
    for action_type in sorted(target_dict):
        handler = target_dict[action_type]
        required = handler.SCHEMA.get("required", [])
        doc = (handler.__doc__ or "").strip().splitlines()[0] if handler.__doc__ else ""
        lines.append(f"- `{action_type}` (risk: {handler.RISK}, payload: {{{', '.join(required)}}}) — {doc}")
    return "\n".join(lines)
