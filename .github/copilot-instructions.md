# PR Intelligence Dashboard Coordination

This workspace currently contains the agent configuration and Phase 1 contracts, not an application implementation. Inspect available source before assuming a framework or adding runtime code.

## Shared workflow

- Follow `.github/agents/pr-intelligence-orchestrator.agent.md` for phase ordering and delegation.
- Treat `docs/pr-intelligence-agent-architecture.md` as the shared contract and acceptance source. Its v0.1 interface is proposed until the GitHub Data and Backend Agent confirms or revises it and the orchestrator records agreement.
- Do not begin full UI implementation until the backend collection/application data contracts are agreed and recorded.
- Specialists must avoid overlapping ownership and report only work actually performed.

## Delegated completion report

Every delegated report must include:

1. Summary
2. Files changed
3. Contracts or definitions changed
4. Exact checks/tests and actual results
5. Assumptions and limitations
6. Blockers
7. Follow-up actions

Do not claim a check, implementation, or handoff unless it actually occurred. Report unavailable source, credentials, permissions, and test infrastructure as blockers rather than inventing successful behavior.
