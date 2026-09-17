# Agent notes

- Read `CONSTRAINTS.md` before changing code. Run `scripts\check.ps1 -Stage task` before calling a task done. Never weaken a constraint to make a change pass.
- Windows PowerShell 5.1 only; tests use Pester 3.4 syntax (`Should Be`, not `Should -Be`).
- Keep the existing dense one-line style in `scripts\`.
