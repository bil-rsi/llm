"""Permission decision chain (Chain of Responsibility). Each link may decide; the first decisive answer wins.

    ToolEnabled → Capability (tool-specific hard limits for the mode) → Rules (DB) → Defaults → Taint escalation

Links only read the PermissionSnapshot; they never widen it. Deny is final at every stage.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from aiplatform.permissions.models import EFFECT_STRENGTH, Decision, PermissionSnapshot, Risk, Rule


@dataclass(frozen=True)
class PermissionRequest:
    """What the engine needs to know about one canonicalised tool call."""

    tool: str
    risk: Risk
    scopes: dict[str, str] = field(default_factory=dict)  # path="projects/app/x.py", domain="github.com", command="ls"
    capability: Decision | None = None  # tool-specific hard-limit verdict (deny/confirm) or None
    taint_sensitive: bool = False  # would exfiltrate/act on tainted input (write/exec/new domain)


CapabilityCheck = Callable[[PermissionRequest, PermissionSnapshot], Decision | None]


def _match_rules(rules: Sequence[Rule], req: PermissionRequest, mode: str) -> Rule | None:
    candidates = [r for r in rules if r.mode in ("*", mode) and r.matches_tool(req.tool) and r.matches_scope(req.scopes)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.priority, r.specificity, EFFECT_STRENGTH[r.effect]))


class PermissionEngine:
    def decide(self, req: PermissionRequest, snap: PermissionSnapshot, *, tainted: bool) -> Decision:
        if not snap.tools_enabled.get(req.tool, True):
            return Decision("deny", "tool_disabled", f"{req.tool} is disabled in the admin console")
        if req.capability is not None and req.capability.effect == "deny":
            return req.capability
        rule = _match_rules(snap.rules, req, snap.mode)
        if rule is not None:
            decision = Decision(rule.effect, f"rule:{rule.id}", rule.note or f"rule {rule.tool_pattern} ({rule.mode})")
        else:
            effect = snap.defaults.get(snap.mode, {}).get(req.risk, "confirm")
            decision = Decision(effect, f"default:{snap.mode}:{req.risk}", f"default for {req.risk} in {snap.mode} mode")
        if decision.effect == "deny":
            return decision
        # A capability 'confirm' (e.g. unlisted domain in trusted mode) can only tighten an allow.
        if (
            req.capability is not None
            and req.capability.effect == "confirm"
            and decision.effect == "allow"
            and snap.mode != "bypass"
        ):
            decision = req.capability
        if snap.taint_escalation and tainted and req.taint_sensitive and decision.effect == "allow":
            decision = Decision("confirm", "taint_escalation", "this turn read untrusted content; confirm before acting on it")
        return decision
