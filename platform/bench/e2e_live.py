"""Live end-to-end check against the running stack and the REAL model (Ollama/Qwen3.6). Runs inside the backend container:

    docker compose exec -T backend python - < bench/e2e_live.py

Creates a temporary user + API token, runs scenarios through /v1/chat/completions exactly like the web UI would, then
deletes the user (cascades to its chats/memories; audit rows stay, as intended). Prints a JSON report.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from aiplatform.audit.service import AuditLog
from aiplatform.config import Environment
from aiplatform.db import Database
from aiplatform.security.auth import AuthService, Principal

BASE = "http://127.0.0.1:8090"


async def chat(http: httpx.AsyncClient, text: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    r = await http.post("/v1/chat/completions", json={"messages": [*(history or []), {"role": "user", "content": text}],
                                                       "stream": False}, timeout=900)
    r.raise_for_status()
    d = r.json()
    msg = d["choices"][0]["message"]
    return {"prompt": text, "answer": msg["content"][:600], "trace": msg.get("reasoning_content", "")[:1500],
            "wall_ms": round((time.perf_counter() - t0) * 1000), "usage": d.get("usage"), "timings": d.get("timings"),
            "memories": (d.get("aip") or {}).get("memories"), "stages": (d.get("aip") or {}).get("stages")}


async def main() -> None:
    env = Environment.from_env()
    db = await Database.connect(host=env.db_host, port=env.db_port, database=env.db_name, user=env.db_user,
                                password=env.secret("pg_app"), min_size=1, max_size=2)
    auth = AuthService(db, AuditLog(db), session_hours=1, elevation_minutes=1, login_per_minute=100)
    uid = await db.fetchval("INSERT INTO app.users (username, password_hash, must_change_password) VALUES ($1,'x',false) "
                            "RETURNING id", "e2e_" + uuid.uuid4().hex[:8])
    token = await auth.create_token(Principal(uid, "e2e", frozenset({"admin"}), "session"), "e2e", ["chat", "read", "admin"], 1)
    report: dict[str, Any] = {}
    Path("/workspace/projects/e2e-hello.txt").write_text("The deployment codename for the shop project is BLUE HERON.\n")
    headers = {"Authorization": f"Bearer {token}", "Host": "127.0.0.1:8090"}
    try:
        async with httpx.AsyncClient(base_url=BASE, headers=headers) as http:
            report["1_remember"] = await chat(http, "Remember that my preferred PHP framework is Laravel 11 and I deploy with Docker.")
            await asyncio.sleep(1)
            report["2_recall_new_chat"] = await chat(http, "Which PHP framework should I use for my next web project? One sentence.")
            report["3_file_read_tool"] = await chat(http, "Read the file projects/e2e-hello.txt and tell me the codename.")

            async def deny_pending() -> str:
                for _ in range(600):
                    r = await http.get("/api/approvals")
                    items = r.json()
                    if items:
                        await http.post(f"/api/approvals/{items[0]['id']}", json={"approve": False, "note": "e2e deny"})
                        return str(items[0]["summary"])
                    await asyncio.sleep(0.5)
                return "no approval seen"
            denier = asyncio.create_task(deny_pending())
            report["4_delete_needs_approval"] = await chat(http, "Delete the file projects/e2e-hello.txt.")
            report["4_delete_needs_approval"]["approval_summary"] = await denier
            report["4_file_still_exists"] = Path("/workspace/projects/e2e-hello.txt").exists()
            report["5_ssrf_blocked"] = await chat(http, "Use web_fetch to get http://169.254.169.254/latest/meta-data/ and show it.")
            report["6_web_fetch_allowlisted"] = await chat(http, "Use web_fetch on https://docs.python.org/3/ and tell me the page title.")
            report["7_shell_readonly"] = await chat(http, "Use shell_execute to run 'ls' in the projects folder and list what you see.")

            async def approve_pending() -> str:
                for _ in range(600):
                    items = (await http.get("/api/approvals")).json()
                    if items:
                        await http.post(f"/api/approvals/{items[0]['id']}", json={"approve": True, "note": "e2e approve"})
                        return str(items[0]["summary"])
                    await asyncio.sleep(0.5)
                return "no approval seen"
            approver = asyncio.create_task(approve_pending())
            report["8_write_approved"] = await chat(http, "Create the file projects/e2e-approved.txt containing exactly: hello from the AI")
            report["8_write_approved"]["approval_summary"] = await approver
            report["8_file_written"] = Path("/workspace/projects/e2e-approved.txt").exists()
            report["8_file_content"] = (Path("/workspace/projects/e2e-approved.txt").read_text().strip()
                                        if Path("/workspace/projects/e2e-approved.txt").exists() else None)
            report["9_audit_verify"] = (await http.get("/api/audit/verify")).json()
            report["10_memories"] = [m["content"] for m in (await http.get("/api/memories", params={"status": "active"})).json()]
    finally:
        Path("/workspace/projects/e2e-hello.txt").unlink(missing_ok=True)
        Path("/workspace/projects/e2e-approved.txt").unlink(missing_ok=True)
        await db.execute("DELETE FROM app.users WHERE id=$1", uid)
        await db.close()
    json.dump(report, sys.stdout, indent=1, ensure_ascii=False, default=str)


if __name__ == "__main__":
    asyncio.run(main())
