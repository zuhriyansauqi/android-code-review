# Android Code Review

A [Hermes Agent](https://github.com/nousresearch/hermes-agent) skill that reviews Android PRs from a GitHub URL — fetches the diff, applies Android-specific checks, and posts inline comments.

## Requirements

- Hermes Agent runtime
- Python 3 (see `.python-version`)
- `GITHUB_TOKEN` environment variable with repo access

## How It Works

Give the agent a GitHub PR URL. The skill will:

1. **Fetch** the PR diff and metadata via `scripts/review_pr.py`
2. **Search** for callers/usages when the diff modifies public APIs (uses GitHub code search)
3. **Review** against an Android-specific checklist defined in `SKILL.md`, covering:
   - Correctness & logic
   - Lifecycle & threading (coroutine scoping, `repeatOnLifecycle`, no `GlobalScope`)
   - ANR & responsiveness (main thread IO, blocking calls, `SharedPreferences.commit`)
   - Memory leaks (static Context refs, unregistered listeners, ViewModel leaks)
   - Jetpack Compose (side-effects, recomposition, state hoisting)
   - Architecture (MVVM/MVI, Clean Architecture, layer boundaries)
   - Security (no hardcoded secrets, input validation)
   - Performance (recomposition, pagination, allocations)
   - Data & persistence (Room migrations, serialization)
   - Testing (coverage, `TestDispatcher`, no flaky patterns)
4. **Post** findings as inline PR comments with an auto-determined verdict and summary

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

## Error Handling

The script retries automatically on transient failures (502, 503, 429, network errors) with backoff. It also handles GitHub's secondary rate limits (403 + `Retry-After`) and proactively pauses when `X-RateLimit-Remaining` is nearly exhausted.

If a request still fails after retries, the agent responds with a clear, actionable message (e.g., invalid token, rate limit, PR not found). See the Error Handling section in `SKILL.md` for the full mapping.

## Project Structure

```
├── SKILL.md             # Agent directive — review workflow and checklist
├── scripts/
│   ├── __init__.py
│   └── review_pr.py     # GitHub API helper (fetch, search, post, fetch-file)
├── tests/
│   ├── __init__.py
│   └── test_review_pr.py
├── pyproject.toml       # Project metadata
├── .python-version      # Python version pin for uv
└── README.md
```

## Testing

```bash
uv run python -m unittest tests.test_review_pr -v
```

## Tags

`Android` `Code-Review` `Pull-Requests` `Kotlin` `Compose` `Architecture` `Quality`

## License

[MIT](LICENSE.md)
