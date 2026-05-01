---
name: android-code-review
description: "Review Android PRs from a GitHub URL: fetch diff, apply Android checks, post inline comments."
version: 1.0.5
author: Mas Ryy
license: MIT
required_environment_variables:
  - GITHUB_TOKEN
metadata:
  hermes:
    tags: [Android, Code-Review, Pull-Requests, Kotlin, Compose, Architecture, Quality]
    related_skills: [github-auth]
---

# Android Code Review

> **AGENT DIRECTIVE**: Follow ONLY the instructions and formats in THIS file. Do NOT invent your own output format.

The user provides a GitHub PR URL. You fetch the diff, review it against the Android checklist, and post findings via the helper script.

---

## Step 1: Fetch PR Data

```bash
python3 ${HERMES_SKILL_DIR}/scripts/review_pr.py fetch "<PR_URL>"
```

This outputs JSON with `pr` (metadata), `files` (changed file list), and `diff` (raw diff text). Read the diff carefully.

## Step 2: Fetch Context Files (if needed)

If the diff references interfaces, base classes, DI modules, or entities you need to understand:

```bash
python3 ${HERMES_SKILL_DIR}/scripts/review_pr.py fetch-file "<PR_URL>" "path/to/File.kt"
```

Prioritize:
- Interfaces / base classes the changed code extends
- DI modules (`@Module`) when new `@Inject` classes appear
- Room entities when migrations or DAOs change
- Nav graph / route constants when navigation changes

**Do NOT fetch the entire repository.** Only files directly referenced by the diff.

## Step 3: Review Against the Checklist

Apply these checks against the diff:

### Correctness & Logic
- Does the code do what it claims?
- Edge cases handled (empty lists, null responses, no network)?
- Error paths handled gracefully?

### Lifecycle & Threading
- No Activity/Fragment/View references in ViewModels
- Coroutines scoped correctly (`viewModelScope`, `lifecycleScope`)
- Flow collected with `repeatOnLifecycle` (not raw `collect` in Fragment/Activity)
- No `GlobalScope` usage
- No `runBlocking` on the main thread
- Heavy work on `Dispatchers.IO` or `Default`, not `Main`
- State survives configuration changes (`rememberSaveable`, ViewModel)

### ANR & Responsiveness
- No network or disk IO on main thread without `withContext(Dispatchers.IO)`
- `SharedPreferences.apply()` over `commit()` (commit blocks the calling thread)
- No heavy work in `onBind`, `onDraw`, or `onMeasure`
- `BroadcastReceiver.onReceive` must not block (10s ANR limit)
- `ContentProvider` queries must not perform synchronous network calls
- No `synchronized` blocks that hold locks across IO operations
- No blocking calls in `Application.onCreate` (delays app start)

### Memory Leaks
- No static references to Activity, Fragment, View, or Context
- Inner classes in Activity/Fragment should be static or use `WeakReference`
- Listeners, callbacks, and observers unregistered in `onDestroy`/`onCleared`/`onDestroyView`
- No Context stored in ViewModel (use `Application` context if needed)
- `DisposableEffect` includes proper cleanup in `onDispose`
- No long-lived coroutines holding references to destroyed scopes

### Jetpack Compose
- Side-effects keyed correctly (`LaunchedEffect`, `DisposableEffect`)
- Lambdas wrapped with `remember` to avoid recomposition
- State hoisting — Composables don't own business state
- `derivedStateOf` used where appropriate
- Types passed to Composables are stable/immutable

### Architecture
- Follows project architecture (MVVM/MVI, Clean Architecture)
- UI layer doesn't call Repository directly
- No Android framework imports in domain layer
- New abstractions justified — flag over-engineering
- No circular dependencies between modules

### Security
- No hardcoded API keys or secrets
- Sensitive data not in logs or plain `SharedPreferences`
- Input validation on deep links and Intent data
- ProGuard/R8 rules updated for new models if needed

### Performance
- No unnecessary recompositions (missing `remember`, unstable types)
- Pagination for list endpoints (`PagingSource`)
- No large allocations in hot paths
- Images loaded with proper sizing

### Data & Persistence
- Room migrations for schema changes (no destructive fallback in prod)
- Serialization annotations correct (`@SerialName`, `@ColumnInfo`)
- No data loss on upgrade path

### Testing
- New logic paths covered by unit tests
- ViewModels tested with `TestDispatcher`
- No flaky patterns (hardcoded delays, uncontrolled dispatchers)

## Step 4: Output Findings as JSON

Write your findings to `/tmp/review_findings.json` using **exactly** this format:

```json
{
  "findings": [
    {
      "file": "app/src/main/java/com/example/feature/LoginViewModel.kt",
      "line": 45,
      "severity": "blocker",
      "title": "Network call on Dispatchers.Main — will cause ANR",
      "why": "Blocks UI thread >5s, triggers ANR dialog",
      "fix": "viewModelScope.launch(Dispatchers.IO) {\n    val result = repository.login(credentials)\n}"
    },
    {
      "file": "app/src/main/java/com/example/feature/UserFragment.kt",
      "line": 23,
      "severity": "warning",
      "title": "Flow collected without repeatOnLifecycle",
      "why": "Keeps collecting when app is backgrounded, wastes resources",
      "fix": "viewLifecycleOwner.lifecycleScope.launch {\n    viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {\n        viewModel.uiState.collect { /* ... */ }\n    }\n}"
    }
  ],
  "looks_good": [
    "Clean ViewModel to UseCase separation",
    "Proper StateFlow with WhileSubscribed(5000)"
  ]
}
```

**Rules:**
- `severity`: one of `blocker`, `warning`, `suggestion`, `nit`
- `line`: the line number from the diff where the issue is (the script auto-snaps to the nearest valid diff line). Set to `0` for general findings that don't target a specific line (e.g., missing unit tests) — these will appear only in the summary, not as inline comments.
- `looks_good`: always include at least one positive observation
- `fix`: optional — include Kotlin code when you have a concrete suggestion
- If no issues found, use empty `findings` array and still include `looks_good`
- **Missing unit tests**: use `line: 0`. Do NOT attach to a random line in the file.

## Step 5: Post the Review

```bash
python3 ${HERMES_SKILL_DIR}/scripts/review_pr.py post "<PR_URL>" /tmp/review_findings.json
```

The script handles everything:
- Validates line numbers against the actual diff
- Chooses verdict automatically (Approve / Request Changes / Comment) based on severities
- Posts atomic review with inline comments
- Posts summary comment using this template:

### Summary Template

```markdown
## Android Code Review Summary

**PR**: {title} (#{number})
**Author**: @{author}
**Verdict**: {verdict}
**Stats**: {changed_files} files changed, +{additions} -{deletions}

### 🔴 Blockers (N)
- **file:line** — title

### 🟡 Warnings (N)
- **file:line** — title

### 💡 Suggestions (N)
- **file:line** — title

### 🟢 Nits (N)
- **file:line** — title

### ✅ Looks Good
- positive observation

---
*Reviewed by Mas Ryy (android-code-review v1.0.5)*
```

> Severity sections with zero items are omitted automatically.

### Verdict Logic (handled by script)
- **Approve** — no blockers, no warnings
- **Request Changes** — any blocker or warning
- **Comment** — only suggestions and nits

## Error Handling

If any script command exits with a non-zero code, **do NOT silently retry or hallucinate results**. Read the stderr output and respond to the user based on the error:

| Error message contains | Tell the user |
|---|---|
| `API error 401` | "GitHub token is invalid or expired. Please check your `GITHUB_TOKEN` or re-authenticate with `gh auth login`." |
| `API error 403` / `rate limit` | "GitHub API rate limit reached. Please wait a few minutes and try again." |
| `API error 404` | "PR not found. Please check the URL — the repo may be private or the PR number may be wrong." |
| `API error 422` | "GitHub rejected the review payload. This is usually a bug in the review script — please report it." |
| `Network error` | "Could not reach GitHub. Please check your internet connection and try again." |
| `No GITHUB_TOKEN` | "No GitHub token found. Set the `GITHUB_TOKEN` environment variable or run `gh auth login`." |
| `Invalid PR URL` | "That doesn't look like a valid GitHub PR URL. Expected format: `https://github.com/owner/repo/pull/123`" |

For any other error, show the stderr output to the user and suggest they retry.

**Never** generate fake review findings if the fetch or post step fails.

## Large PRs (>50 files)

Ask the user whether to:
- Review in batches (by module or feature)
- Focus on specific areas (e.g., "just threading and lifecycle")
