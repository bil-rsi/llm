"""Shell command rules shared by the backend (permission decision) and the tool-runner (re-validation).

Standard library only: this file is copied into the tool-runner image. Restricted mode = argv only (no shell), an
allowlist of read-only commands, and per-command argument validators. Broad mode (Bypass + broad_shell) allows any
command, including `sh -c`, but still only inside the network-less, secret-less runner container.
"""

from __future__ import annotations

import re

MAX_ARGS = 64
MAX_ARG_LEN = 4096

# command -> set of rejected flags / subcommand policy
ALLOWED_COMMANDS = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "tree",
    "file",
    "stat",
    "du",
    "diff",
    "sort",
    "uniq",
    "cut",
    "tr",
    "md5sum",
    "sha256sum",
    "basename",
    "dirname",
    "realpath",
    "pwd",
    "echo",
    "date",
    "git",
    "jq",
    "less",
}
_FIND_FORBIDDEN = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
_GIT_READ_SUBCOMMANDS = {
    "status",
    "log",
    "diff",
    "show",
    "ls-files",
    "blame",
    "branch",
    "rev-parse",
    "describe",
    "shortlog",
    "grep",
    "tag",
}
_GIT_FORBIDDEN_GLOBAL = re.compile(r"^(-c|--config-env|--exec-path|--git-dir|--work-tree|--namespace|-C)(=|$)")
_SORT_FORBIDDEN = re.compile(r"^(-o|--output|--compress-program)(=|$)")
_OUTPUT_FLAGS = re.compile(r"^--?(output|out|o)(=|$)")


class CommandRejected(ValueError):
    pass


def validate_restricted(argv: list[str]) -> None:
    """Raise CommandRejected unless argv is an allowlisted, read-only invocation."""
    if not argv:
        raise CommandRejected("empty command")
    if len(argv) > MAX_ARGS or any(len(a) > MAX_ARG_LEN or "\x00" in a for a in argv):
        raise CommandRejected("too many or too long arguments")
    cmd = argv[0]
    if "/" in cmd or cmd not in ALLOWED_COMMANDS:
        raise CommandRejected(f"'{cmd}' is not in the read-only command allowlist ({', '.join(sorted(ALLOWED_COMMANDS))})")
    args = argv[1:]
    if cmd == "find" and any(a in _FIND_FORBIDDEN for a in args):
        raise CommandRejected("find actions (-exec, -delete, -fprint...) are not allowed")
    if cmd == "git":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if _GIT_FORBIDDEN_GLOBAL.match(args[i]):
                raise CommandRejected(f"git option {args[i]} is not allowed")
            i += 1
        if i >= len(args) or args[i] not in _GIT_READ_SUBCOMMANDS:
            raise CommandRejected(f"only read-only git subcommands are allowed: {', '.join(sorted(_GIT_READ_SUBCOMMANDS))}")
        rest = args[i + 1 :]
        if any(a.startswith("--output") or a == "--ext-diff" or a.startswith("--textconv") for a in rest):
            raise CommandRejected("git output redirection / external diff is not allowed")
        if args[i] in ("branch", "tag") and any(
            not a.startswith("-") or a in ("-d", "-D", "-m", "-M", "-f")
            for a in rest
            if a not in ("-a", "-r", "-l", "--list", "-v", "-vv")
        ):
            raise CommandRejected("git branch/tag may only list")
    if cmd == "sort" and any(_SORT_FORBIDDEN.match(a) for a in args):
        raise CommandRejected("sort output files are not allowed")
    if any(_OUTPUT_FLAGS.match(a) for a in args if cmd not in ("grep", "git")):
        raise CommandRejected("output-file flags are not allowed")


def command_name(argv: list[str]) -> str:
    return argv[0] if argv else ""
