# Contributing to Hermes Evolution

This is the `Lexus2016/hermes-agent-evolution` fork of Hermes Agent. Open issues
and pull requests against this fork, not `NousResearch/hermes-agent`.

## Start here

- Read [AGENTS.md](AGENTS.md) for repository-wide rules and area-specific guides.
- Follow [CONTRIBUTING.md](CONTRIBUTING.md#development-setup) for the current
  development and separate test environments.
- Use the [PM developer workflow](website/docs/reference/package-management.md#developer-workflow)
  to activate an environment with isolated user state. Do not modify a normal
  Hermes installation with ad hoc dependency installs.
- Read [EVOLUTION_README.md](EVOLUTION_README.md) for the modes and scheduled jobs.
- Read [SECURITY.md](SECURITY.md) before reporting a potential vulnerability.
  Use the private reporting route for findings within its vulnerability scope.

## Propose a change

Search [existing issues](https://github.com/Lexus2016/hermes-agent-evolution/issues)
and reproduce the problem on current `main`. Include the exact version or commit,
environment, expected behavior and a minimal reproduction. Redact credentials,
conversation transcripts and private files.

For an evolution change, explain the affected mode, required permissions, external
actions, data sent outside the machine, and recovery behavior. Distinguish a
proposal, an implemented feature and a verified result.

## Implement and verify

1. Branch from current `main` and keep the change focused.
2. Read the area's `AGENTS.md` before editing that area.
3. Add behavior tests that reproduce the issue. Use isolated `HERMES_HOME` and
   runtime directories as described in the developer workflow.
4. Run tests through `scripts/run_tests.sh`, using the test environment and
   options documented in `CONTRIBUTING.md`. Do not substitute bare `pytest`.
5. Run `python scripts/check` for the blocking lint checks.
6. Update the relevant user and developer documentation.

Do not invoke live evolution jobs, provider requests, publishing, self-update or
installation scripts as a routine offline documentation check. Those actions can
change user state, contact external services or incur charges.

## Submit to this fork

Use `Lexus2016/hermes-agent-evolution` as the explicit base repository. A local
clone's GitHub CLI default can otherwise resolve to the upstream parent. See
[the targeting instructions](AGENTS.md#fork--pr-targeting-read-before-opening-any-pr).

In the pull request, include:

- The problem, related issue and scope of the change.
- Affected modes, permissions and compatibility risks.
- Exact verification commands and results, including failures and skips.
- Recovery steps when the change affects updates or persistent state.

Follow the current review and contribution guidance in [CONTRIBUTING.md](CONTRIBUTING.md).
Do not infer approval to merge or deploy from a passing test run.
