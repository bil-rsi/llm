"""Permission engine: modes, rule precedence, capabilities, taint escalation, disabled tools."""

from __future__ import annotations

from aiplatform.permissions.engine import PermissionEngine, PermissionRequest
from aiplatform.permissions.models import Decision
from tests.conftest import rule, snapshot

E = PermissionEngine()


def req(tool: str, risk: str, path: str | None = None, **kw: object) -> PermissionRequest:
    return PermissionRequest(tool, risk, {"path": path} if path else {}, **kw)  # type: ignore[arg-type]


def test_defaults_per_mode() -> None:
    assert E.decide(req("filesystem.read", "read"), snapshot("normal"), tainted=False).effect == "allow"
    assert E.decide(req("filesystem.write", "write"), snapshot("normal"), tainted=False).effect == "confirm"
    assert E.decide(req("filesystem.delete", "destructive"), snapshot("autonomous"), tainted=False).effect == "deny"
    assert E.decide(req("filesystem.delete", "destructive"), snapshot("bypass"), tainted=False).effect == "allow"


def test_rules_match_scope_and_priority() -> None:
    rules = (
        rule("filesystem.*", "autonomous", "allow", scope=("path", "sandbox/**"), priority=200),
        rule("filesystem.delete", "autonomous", "confirm", scope=("path", "projects/**"), priority=150),
    )
    s = snapshot("autonomous", rules=rules)
    assert E.decide(req("filesystem.delete", "destructive", "sandbox/a/b.txt"), s, tainted=False).effect == "allow"
    assert E.decide(req("filesystem.delete", "destructive", "projects/x.txt"), s, tainted=False).effect == "confirm"
    assert E.decide(req("filesystem.delete", "destructive", "allowed/x.txt"), s, tainted=False).effect == "deny"


def test_deny_wins_ties() -> None:
    rules = (rule("web.fetch", "*", "allow"), rule("web.fetch", "*", "deny"))
    assert E.decide(req("web.fetch", "network"), snapshot(rules=rules), tainted=False).effect == "deny"


def test_capability_deny_is_final_even_in_bypass() -> None:
    cap = Decision("deny", "capability:read_only_root", "read-only")
    s = snapshot("bypass", rules=(rule("*", "bypass", "allow", priority=999),))
    assert E.decide(req("filesystem.write", "write", "allowed/x", capability=cap), s, tainted=False).effect == "deny"


def test_capability_confirm_tightens_allow_but_not_in_bypass() -> None:
    cap = Decision("confirm", "capability:unlisted_domain", "not listed")
    assert E.decide(req("web.fetch", "network", capability=cap), snapshot("normal"), tainted=False).effect == "confirm"
    assert E.decide(req("web.fetch", "network", capability=cap), snapshot("bypass"), tainted=False).effect == "allow"


def test_taint_escalation_only_when_enabled() -> None:
    r = req("filesystem.write", "write", "sandbox/x", taint_sensitive=True)
    s_on = snapshot("bypass", taint=True)
    assert E.decide(r, s_on, tainted=True).effect == "confirm"
    assert E.decide(r, s_on, tainted=False).effect == "allow"
    assert E.decide(r, snapshot("bypass", taint=False), tainted=True).effect == "allow"


def test_disabled_tool_denied() -> None:
    s = snapshot("bypass", tools_enabled={"shell.execute": False})
    assert E.decide(req("shell.execute", "execute"), s, tainted=False).effect == "deny"


def test_glob_semantics() -> None:
    s = snapshot("normal", rules=(rule("filesystem.write", "normal", "allow", scope=("path", "projects/app/*.py")),))
    assert E.decide(req("filesystem.write", "write", "projects/app/main.py"), s, tainted=False).effect == "allow"
    assert E.decide(req("filesystem.write", "write", "projects/app/sub/main.py"), s, tainted=False).effect == "confirm"
