# Android Code Review

Review Android PRs from a GitHub URL: fetch diff, apply Android-specific checks, and post inline comments.

## Requirements

- Python 3 (see `.python-version`)
- `GITHUB_TOKEN` environment variable with repo access

## Usage

Provide a GitHub PR URL and the skill will:

1. **Fetch** the PR diff and metadata
2. **Review** against an Android-specific checklist covering:
   - Correctness & logic
   - Lifecycle & threading (coroutine scoping, `repeatOnLifecycle`, no `GlobalScope`)
   - Jetpack Compose (side-effects, recomposition, state hoisting)
   - Architecture (MVVM/MVI, Clean Architecture, layer boundaries)
   - Security (no hardcoded secrets, input validation)
   - Performance (recomposition, pagination, allocations)
   - Data & persistence (Room migrations, serialization)
   - Testing (coverage, `TestDispatcher`, no flaky patterns)
3. **Post** findings as inline PR comments with an auto-determined verdict

### Verdict Logic

| Condition | Verdict |
|---|---|
| No blockers or warnings | ✅ Approve |
| Any blocker or warning | ❌ Request Changes |
| Only suggestions/nits | 💬 Comment |

## Severity Levels

- **blocker** — Must fix before merge
- **warning** — Should fix, potential issue
- **suggestion** — Improvement idea
- **nit** — Style/preference

## Tags

`Android` `Code-Review` `Pull-Requests` `Kotlin` `Compose` `Architecture` `Quality`

## License

MIT
