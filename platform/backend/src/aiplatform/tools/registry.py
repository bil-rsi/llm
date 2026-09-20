"""Tool registry (code is the source of the catalogue; `tool_definitions` mirrors it for the admin console)."""

from __future__ import annotations

from typing import Any

from aiplatform.db import Database
from aiplatform.model.types import ToolSpec
from aiplatform.permissions.models import PermissionSnapshot
from aiplatform.tools.base import Tool, json_schema, model_tool_name


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._by_name: dict[str, Tool] = {}
        for t in tools:
            if t.name in self._by_name:
                raise ValueError(f"duplicate tool {t.name}")
            self._by_name[t.name] = t
        self._by_model_name = {model_tool_name(n): t for n, t in self._by_name.items()}

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name) or self._by_model_name.get(name)

    def all(self) -> list[Tool]:
        return list(self._by_name.values())

    def specs_for(self, snap: PermissionSnapshot) -> list[ToolSpec]:
        """Tool specs offered to the model: enabled tools that are not denied outright in the current mode."""
        out: list[ToolSpec] = []
        for t in self._by_name.values():
            if not snap.tools_enabled.get(t.name, True):
                continue
            if t.category == "web" and snap.effective_internet_mode() == "disabled":
                continue
            blanket_deny = any(
                r.effect == "deny"
                and r.scope_type == "any"
                and r.mode in ("*", snap.mode)
                and r.matches_tool(t.name)
                and r.priority >= 100
                for r in snap.rules
            )
            if blanket_deny:
                continue
            out.append(ToolSpec(model_tool_name(t.name), t.description, json_schema(t.Args)))
        return out

    async def sync_definitions(self, db: Database) -> None:
        rows: list[tuple[Any, ...]] = [(t.name, t.category, t.description, t.risk, json_schema(t.Args)) for t in self.all()]
        async with db.transaction() as tx:
            await tx.executemany(
                "INSERT INTO app.tool_definitions (name, category, description, risk, input_schema) VALUES ($1,$2,$3,$4,$5) "
                "ON CONFLICT (name) DO UPDATE SET category=EXCLUDED.category, description=EXCLUDED.description, "
                "risk=EXCLUDED.risk, input_schema=EXCLUDED.input_schema, version=app.tool_definitions.version + "
                "(CASE WHEN app.tool_definitions.input_schema IS DISTINCT FROM EXCLUDED.input_schema THEN 1 ELSE 0 END)",
                rows,
            )
            await tx.execute("DELETE FROM app.tool_definitions WHERE NOT (name = ANY($1::text[]))", [r[0] for r in rows])
