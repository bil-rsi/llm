"""Shell allowlist (restricted mode): command injection and argument injection are rejected."""

from __future__ import annotations

import pytest

from aiplatform.tools.shell.rules import CommandRejected, validate_restricted

ALLOWED = [
    ["ls", "-la"],
    ["git", "log", "-5", "--oneline"],
    ["grep", "-rn", "TODO", "."],
    ["find", ".", "-name", "*.py"],
    ["cat", "README.md"],
    ["git", "diff", "HEAD~1"],
    ["wc", "-l", "a.txt"],
    ["git", "branch", "-a"],
]
REJECTED = [
    ["rm", "-rf", "/"],
    ["sh", "-c", "ls; rm -rf /"],
    ["bash", "-c", "id"],
    ["/bin/ls"],
    ["python3", "-c", "print(1)"],
    ["curl", "http://evil"],
    ["wget", "x"],
    ["find", ".", "-exec", "rm", "{}", ";"],
    ["find", ".", "-delete"],
    ["find", ".", "-fprint", "/tmp/x"],
    ["git", "-c", "core.sshCommand=touch /tmp/p", "status"],
    ["git", "push"],
    ["git", "--git-dir=/etc", "log"],
    ["git", "config", "--global", "x", "y"],
    ["git", "branch", "-D", "main"],
    ["git", "branch", "newbranch"],
    ["git", "diff", "--output=/tmp/x"],
    ["sort", "-o", "/tmp/x", "a"],
    ["chmod", "777", "x"],
    ["dd", "if=/dev/zero"],
    [],
    ["ls"] * 70,
    ["cat", "a\x00b"],
]


@pytest.mark.parametrize("argv", ALLOWED)
def test_allowed(argv: list[str]) -> None:
    validate_restricted(argv)


@pytest.mark.parametrize("argv", REJECTED)
def test_rejected(argv: list[str]) -> None:
    with pytest.raises(CommandRejected):
        validate_restricted(argv)


def test_metacharacters_are_inert_arguments() -> None:
    # No shell is involved: `;`, `$()`, backticks and `&&` are passed literally to ls, which just fails to find them.
    validate_restricted(["ls", "; rm -rf /", "$(id)", "`id`", "&&", "|"])
