# Software Configuration Management plan

The plan V-222632 requires, describing the process actually in force for
`opscontinuum/picoagent` - not an aspiration. Each numbered section is one of the elements
the rule's check enumerates; where practice changes, this document changes in the same pull
request, because a plan that trails reality fails the rule's own purpose.

## 1. Configuration control and change management process

Every change lands through the same path: a branch, a pull request against `main`, review,
and a squash merge producing one commit per pull request titled `Sentence (#N)`. `main` is
never pushed to directly. Work performed by agents runs in git worktrees under
`.claude/worktrees/` so parallel work cannot contaminate the checkout; `git stash` is not
used (the stack is shared across worktrees), and staging is by named file, never `git add
-A`. Security-relevant changes additionally update the threat model or this assessment
record in the same pull request (the threat model's own review triggers, quoted at
V-222651 in the addendum).

## 2. Types of objects developed

Python source (the `picoagent/` package and the two provider references in
`examples/plugins/`), the test suite (`tests/`), documentation (`docs/`, README,
SECURITY.md), tooling (`tools/`), and packaging metadata (`pyproject.toml`). Plugins beyond
the two references are separate repositories with their own configuration management
(`picoagent-plugins`, `es-doctor`, `stig-runner`, `iscp-author`).

## 3. Roles and responsibilities of the organization

A single-maintainer project. The maintainer (`thanatostyrannos`) holds every role below;
the point of writing them down anyway is that each is a distinct duty with distinct
evidence, and a future second contributor inherits a defined seat rather than an ambiguity.

## 4. Defined responsibilities

- **Maintainer / CCB chair**: authorizes every release unit by merging its pull request
  (V-222633); owns the trust decisions in `~/.picoagent` stores.
- **Designated security tester** (V-222646): the maintainer, supplemented per review round
  by adversarial review agents tasked to break fixes rather than approve them - the
  recorded practice of PR #25's two review rounds.
- **Vulnerability response** (V-222657): the maintainer, per SECURITY.md.

## 5. Actions to be performed

Before merge: full suite green (`python3 -m unittest discover -s tests`), under a plain and
a symlinked `TMPDIR`; new guard or security tests proved by planting the defect (red) and
restoring (green); documentation that describes changed behaviour updated in the same
change. At release: the coverage statistic recorded per release in
`docs/testing-and-conventions.md`; determinations walked per
`tools/stig/README.md`. Quarterly: CM access review recorded in the assessment addendum
(V-222631); DISA maintenance-window check for a new ASD release.

## 6. Tools used in the process

git; GitHub (pull requests, review, squash merge, private vulnerability reporting); the
standard-library test runner (`unittest`); `tools/coverage_report.py` (stdlib `trace`);
`tools/stig/` (checklist generation and validation); CI as configured under
`.github/workflows/` (security scanning, SonarCloud). Third-party tools are pinned by the
workflow files that invoke them, which are themselves under configuration control.

## 7. Techniques and methodologies

Test-driven changes with red/green proof as the standard of evidence; adversarial
verification for security fixes; append-only records (session logs, this assessment
lineage) with annotation rather than rewriting; trust-on-approval for plugin code with
per-file fingerprints (`docs/security/trust-boundaries.md`). Simultaneous-update control
(the check's named concern) is git's merge machinery plus the one-commit-per-PR discipline;
the repository host refuses non-fast-forward pushes to `main`.

## 8. Initial set of baselined software components

The baseline is any tagged or merged state of `main`; each squash commit is a recreatable
release unit (`git checkout <commit>` reproduces it bit-for-bit, and a clone plus CPython
>= 3.11 runs it with no build step - the reproducible-build demonstration the check asks
for). Component inventory at baseline: CPython >= 3.11 and git; zero third-party runtime
dependencies (`pyproject.toml`).
