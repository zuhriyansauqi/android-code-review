#!/usr/bin/env python3
"""
Android PR Review — GitHub API helper.

Usage:
  # Fetch PR diff + metadata (agent reads this, then reviews)
  python review_pr.py fetch <pr_url>

  # Post review from agent's JSON findings
  python review_pr.py post <pr_url> <findings_json_file>

Requires: GITHUB_TOKEN env var or gh CLI authenticated.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

VERSION = "1.0.3"
API_TIMEOUT = 30
MAX_LINE_SNAP_DISTANCE = 20

_token_cache = None


def get_token():
    global _token_cache
    if _token_cache:
        return _token_cache
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        _token_cache = token
        return token
    # Try gh CLI
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        )
        _token_cache = result.stdout.strip()
        return _token_cache
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    print("Error: No GITHUB_TOKEN and gh CLI not authenticated.", file=sys.stderr)
    sys.exit(1)


def parse_pr_url(url):
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        print(f"Error: Invalid PR URL: {url}", file=sys.stderr)
        sys.exit(1)
    return m.group(1), m.group(2), int(m.group(3))


def api_request(path, method="GET", body=None, accept=None, max_retries=3):
    token = get_token()
    headers = {"Authorization": f"token {token}", "User-Agent": "hermes-android-review"}
    if accept:
        headers["Accept"] = accept
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = f"https://api.github.com{path}" if path.startswith("/") else path
    is_diff = accept == "application/vnd.github.v3.diff"

    for attempt in range(max_retries):
        try:
            data = json.dumps(body).encode() if body else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                raw = resp.read().decode()
                if is_diff:
                    return raw
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            if attempt < max_retries - 1 and e.code in (502, 503, 429):
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after else (attempt + 1) * 2
                else:
                    delay = (attempt + 1) * 2
                time.sleep(delay)
                continue
            print(f"API error {e.code}: {err_body}", file=sys.stderr)
            sys.exit(1)

    print(f"API request failed after {max_retries} retries: {url}", file=sys.stderr)
    sys.exit(1)


def fetch_pr_files(owner, repo, pr_number):
    """Fetch all changed files with pagination."""
    files = []
    page = 1
    per_page = 100
    while True:
        batch = api_request(f"/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page={per_page}&page={page}")
        if not batch:
            break
        files.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return files


def parse_diff_lines(diff_text):
    """Parse diff to map file+line to valid commentable lines.

    Returns dict: { "path": { line_number: "LEFT"|"RIGHT" } }
    """
    valid = {}
    current_file = None
    old_line = new_line = 0

    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            m = re.search(r" b/(.+)$", line)
            if m:
                current_file = m.group(1)
                valid[current_file] = {}
        elif line.startswith("@@") and current_file:
            m = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
        elif current_file and current_file in valid:
            if line.startswith("+") and not line.startswith("+++"):
                valid[current_file][new_line] = "RIGHT"
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                valid[current_file][old_line] = "LEFT"
                old_line += 1
            elif not line.startswith("\\"):
                # Context lines are valid on both sides; prefer RIGHT for commenting
                valid[current_file][new_line] = "RIGHT"
                old_line += 1
                new_line += 1

    return valid


def find_nearest_valid_line(valid_lines, target_line):
    """Find the closest valid line number to the target within MAX_LINE_SNAP_DISTANCE."""
    if not valid_lines:
        return None, None
    if target_line in valid_lines:
        return target_line, valid_lines[target_line]
    lines = sorted(valid_lines.keys())
    closest = min(lines, key=lambda ln: abs(ln - target_line))
    if abs(closest - target_line) > MAX_LINE_SNAP_DISTANCE:
        return None, None
    return closest, valid_lines[closest]


def cmd_fetch(pr_url):
    owner, repo, pr_number = parse_pr_url(pr_url)

    pr = api_request(f"/repos/{owner}/{repo}/pulls/{pr_number}")
    files = fetch_pr_files(owner, repo, pr_number)
    diff = api_request(
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )

    output = {
        "pr": {
            "number": pr_number,
            "title": pr["title"],
            "author": pr["user"]["login"],
            "body": pr.get("body", ""),
            "head_sha": pr["head"]["sha"],
            "head_ref": pr["head"]["ref"],
            "base_ref": pr["base"]["ref"],
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "changed_files": pr["changed_files"],
        },
        "files": [
            {
                "path": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
            }
            for f in files
        ],
        "diff": diff,
    }

    print(json.dumps(output, indent=2))


def cmd_post(pr_url, findings_file):
    owner, repo, pr_number = parse_pr_url(pr_url)

    with open(findings_file) as f:
        findings = json.load(f)

    pr = api_request(f"/repos/{owner}/{repo}/pulls/{pr_number}")
    head_sha = pr["head"]["sha"]

    diff = api_request(
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )
    valid_lines = parse_diff_lines(diff)

    # Build inline comments with validated line numbers
    comments = []
    for finding in findings.get("findings", []):
        path = finding["file"]
        line = finding["line"]
        severity = finding["severity"]
        title = finding["title"]
        why = finding.get("why", "")
        fix = finding.get("fix", "")

        emoji = {"blocker": "🔴", "warning": "🟡", "suggestion": "💡", "nit": "🟢"}.get(
            severity, "💡"
        )
        label = severity.capitalize()

        body = f"{emoji} **{label}**: {title}"
        if why:
            body += f"\n\n**Why**: {why}"
        if fix:
            body += f"\n\n**Fix**:\n```kotlin\n{fix}\n```"

        file_lines = valid_lines.get(path, {})

        # line 0 = general finding (e.g., missing tests), summary only
        if line == 0:
            continue

        actual_line, side = find_nearest_valid_line(file_lines, line)

        if actual_line is None:
            print(f"Warning: No valid diff line for {path}:{line} (skipped, nearest line too far or not in diff).", file=sys.stderr)
            continue

        comment = {"path": path, "line": actual_line, "side": side, "body": body}
        comments.append(comment)

    # Determine verdict
    severities = [f["severity"] for f in findings.get("findings", [])]
    if any(s in ("blocker", "warning") for s in severities):
        event = "REQUEST_CHANGES"
    elif severities:
        event = "COMMENT"
    else:
        event = "APPROVE"

    # Post atomic review
    review = {
        "commit_id": head_sha,
        "event": event,
        "body": "See inline comments." if comments else "No issues found.",
        "comments": comments,
    }
    api_request(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", method="POST", body=review)
    print(f"Posted review: {event} with {len(comments)} inline comment(s).")

    # Post summary comment
    summary = build_summary(findings, pr, event)
    api_request(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", method="POST", body={"body": summary})
    print("Posted summary comment.")


def build_summary(findings, pr, event):
    verdict_map = {
        "APPROVE": "✅ Approve",
        "REQUEST_CHANGES": "🔄 Request Changes",
        "COMMENT": "💬 Comment Only",
    }

    lines = [
        "## Android Code Review Summary",
        "",
        f"**PR**: {pr['title']} (#{pr['number']})",
        f"**Author**: @{pr['user']['login']}",
        f"**Verdict**: {verdict_map[event]}",
        f"**Stats**: {pr['changed_files']} files changed, +{pr['additions']} -{pr['deletions']}",
        "",
    ]

    groups = {"blocker": [], "warning": [], "suggestion": [], "nit": []}
    for f in findings.get("findings", []):
        groups.get(f["severity"], []).append(f)

    section_map = [
        ("blocker", "🔴 Blockers"),
        ("warning", "🟡 Warnings"),
        ("suggestion", "💡 Suggestions"),
        ("nit", "🟢 Nits"),
    ]

    for key, header in section_map:
        items = groups[key]
        if items:
            lines.append(f"### {header} ({len(items)})")
            for item in items:
                lines.append(f"- **{item['file']}:{item['line']}** — {item['title']}")
            lines.append("")

    # Looks Good section
    lines.append("### ✅ Looks Good")
    for note in findings.get("looks_good", ["No major issues outside the findings above"]):
        lines.append(f"- {note}")
    lines.append("")
    lines.append("---")
    lines.append(f"*Reviewed by Mas Ryy (android-code-review v{VERSION})*")

    return "\n".join(lines)


def cmd_fetch_file(pr_url, file_path):
    """Fetch a single file from the PR branch."""
    owner, repo, pr_number = parse_pr_url(pr_url)
    pr = api_request(f"/repos/{owner}/{repo}/pulls/{pr_number}")
    ref = pr["head"]["ref"]
    content = api_request(f"/repos/{owner}/{repo}/contents/{file_path}?ref={ref}")
    if not isinstance(content, dict):
        print(f"Error: Unexpected response for {file_path}", file=sys.stderr)
        sys.exit(1)
    encoding = content.get("encoding", "")
    if encoding == "base64":
        print(base64.b64decode(content["content"]).decode())
    elif content.get("download_url"):
        raw = api_request(content["download_url"], accept="application/octet-stream")
        print(raw)
    else:
        print(content.get("content", ""))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "fetch":
        cmd_fetch(sys.argv[2])
    elif cmd == "post":
        if len(sys.argv) < 4:
            print("Usage: review_pr.py post <pr_url> <findings.json>", file=sys.stderr)
            sys.exit(1)
        cmd_post(sys.argv[2], sys.argv[3])
    elif cmd == "fetch-file":
        if len(sys.argv) < 4:
            print("Usage: review_pr.py fetch-file <pr_url> <file_path>", file=sys.stderr)
            sys.exit(1)
        cmd_fetch_file(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
