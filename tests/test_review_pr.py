"""Tests for scripts/review_pr.py"""

import io
import os
import time
import unittest
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import scripts.review_pr as review_pr
from scripts.review_pr import (
    ReviewError,
    _check_rate_limit,
    _update_rate_limit,
    api_request,
    build_summary,
    find_nearest_valid_line,
    get_token,
    parse_diff_lines,
    parse_pr_url,
)

SAMPLE_DIFF = """\
diff --git a/app/src/main/java/com/example/Foo.kt b/app/src/main/java/com/example/Foo.kt
--- a/app/src/main/java/com/example/Foo.kt
+++ b/app/src/main/java/com/example/Foo.kt
@@ -10,6 +10,8 @@ class Foo {
     val x = 1
+    val y = 2
+    val z = 3
     val w = 4
-    val old = 5
     val end = 6
"""

SAMPLE_MULTI_FILE_DIFF = """\
diff --git a/A.kt b/A.kt
--- a/A.kt
+++ b/A.kt
@@ -1,3 +1,4 @@
 line1
+added
 line3
diff --git a/B.kt b/B.kt
--- a/B.kt
+++ b/B.kt
@@ -5,3 +5,4 @@
 old
+new
 end
"""


class TestParseUrl(unittest.TestCase):
    def test_valid_url(self):
        owner, repo, num = parse_pr_url("https://github.com/user/repo/pull/42")
        self.assertEqual(owner, "user")
        self.assertEqual(repo, "repo")
        self.assertEqual(num, 42)

    def test_invalid_url(self):
        with self.assertRaises(ReviewError):
            parse_pr_url("https://gitlab.com/user/repo/pull/1")

    def test_not_a_url(self):
        with self.assertRaises(ReviewError):
            parse_pr_url("not-a-url")

    def test_missing_pr_number(self):
        with self.assertRaises(ReviewError):
            parse_pr_url("https://github.com/user/repo/pull/")


class TestGetToken(unittest.TestCase):
    def setUp(self):
        review_pr._token_cache = None

    def tearDown(self):
        review_pr._token_cache = None

    @patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"})
    def test_from_env(self):
        self.assertEqual(get_token(), "test-token")

    @patch.dict(os.environ, {}, clear=True)
    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_no_token_raises(self, _mock_run):
        # Also clear GITHUB_TOKEN if present
        os.environ.pop("GITHUB_TOKEN", None)
        with self.assertRaises(ReviewError) as ctx:
            get_token()
        self.assertIn("No GITHUB_TOKEN", str(ctx.exception))

    @patch.dict(os.environ, {"GITHUB_TOKEN": "cached"})
    def test_caches_token(self):
        get_token()
        self.assertEqual(review_pr._token_cache, "cached")
        # Second call uses cache
        self.assertEqual(get_token(), "cached")


class TestParseDiffLines(unittest.TestCase):
    def test_single_file(self):
        result = parse_diff_lines(SAMPLE_DIFF)
        self.assertIn("app/src/main/java/com/example/Foo.kt", result)
        entry = result["app/src/main/java/com/example/Foo.kt"]
        line_map = entry["map"]
        # +    val y = 2  → line 11 RIGHT
        self.assertEqual(line_map[11], "RIGHT")
        # -    val old = 5 → line 12 LEFT (deleted line on old side)
        self.assertEqual(line_map[12], "LEFT")
        # sorted list is pre-computed
        self.assertEqual(entry["sorted"], sorted(entry["map"]))

    def test_multi_file(self):
        result = parse_diff_lines(SAMPLE_MULTI_FILE_DIFF)
        self.assertIn("A.kt", result)
        self.assertIn("B.kt", result)
        self.assertEqual(result["A.kt"]["map"][2], "RIGHT")
        self.assertEqual(result["B.kt"]["map"][6], "RIGHT")

    def test_empty_diff(self):
        result = parse_diff_lines("")
        self.assertEqual(result, {})

    def test_context_lines_are_right(self):
        result = parse_diff_lines(SAMPLE_DIFF)
        entry = result["app/src/main/java/com/example/Foo.kt"]
        # Context line "val x = 1" at new line 10
        self.assertEqual(entry["map"][10], "RIGHT")


class TestFindNearestValidLine(unittest.TestCase):
    def _entry(self, lines_dict):
        return {"map": lines_dict, "sorted": sorted(lines_dict)}

    def test_exact_match(self):
        entry = self._entry({10: "RIGHT", 20: "RIGHT"})
        line, side = find_nearest_valid_line(entry, 10)
        self.assertEqual(line, 10)
        self.assertEqual(side, "RIGHT")

    def test_snap_to_nearest(self):
        entry = self._entry({10: "RIGHT", 30: "LEFT"})
        line, side = find_nearest_valid_line(entry, 12)
        self.assertEqual(line, 10)

    def test_snap_up(self):
        entry = self._entry({10: "RIGHT", 15: "LEFT"})
        line, side = find_nearest_valid_line(entry, 13)
        self.assertEqual(line, 15)

    def test_too_far_returns_none(self):
        entry = self._entry({10: "RIGHT"})
        line, side = find_nearest_valid_line(entry, 50)
        self.assertIsNone(line)
        self.assertIsNone(side)

    def test_empty_entry(self):
        line, side = find_nearest_valid_line(None, 10)
        self.assertIsNone(line)

    def test_empty_map(self):
        line, side = find_nearest_valid_line({"map": {}, "sorted": []}, 10)
        self.assertIsNone(line)

    def test_within_max_distance(self):
        entry = self._entry({100: "RIGHT"})
        # MAX_LINE_SNAP_DISTANCE is 20
        line, _ = find_nearest_valid_line(entry, 120)
        self.assertEqual(line, 100)

    def test_beyond_max_distance(self):
        entry = self._entry({100: "RIGHT"})
        line, _ = find_nearest_valid_line(entry, 121)
        self.assertIsNone(line)


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        review_pr._rate_limit_remaining = None
        review_pr._rate_limit_reset = None

    def tearDown(self):
        review_pr._rate_limit_remaining = None
        review_pr._rate_limit_reset = None

    def test_update_from_headers(self):
        headers = {"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1700000000"}
        _update_rate_limit(headers)
        self.assertEqual(review_pr._rate_limit_remaining, 42)
        self.assertEqual(review_pr._rate_limit_reset, 1700000000)

    def test_update_ignores_missing_headers(self):
        _update_rate_limit({})
        self.assertIsNone(review_pr._rate_limit_remaining)

    @patch("time.sleep")
    def test_check_sleeps_when_exhausted(self, mock_sleep):
        review_pr._rate_limit_remaining = 1
        review_pr._rate_limit_reset = time.time() + 5
        _check_rate_limit()
        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        self.assertGreater(delay, 4)

    @patch("time.sleep")
    def test_check_no_sleep_when_remaining(self, mock_sleep):
        review_pr._rate_limit_remaining = 100
        review_pr._rate_limit_reset = time.time() + 60
        _check_rate_limit()
        mock_sleep.assert_not_called()


class TestApiRequest(unittest.TestCase):
    def setUp(self):
        review_pr._token_cache = "fake-token"
        review_pr._rate_limit_remaining = None
        review_pr._rate_limit_reset = None

    def tearDown(self):
        review_pr._token_cache = None

    @staticmethod
    def _headers(**kwargs):
        """Build an email.message.Message with the given key-value pairs."""
        m = Message()
        for k, v in kwargs.items():
            m[k] = v
        return m

    @patch("urllib.request.urlopen")
    def test_success_json(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.headers = {}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = api_request("/test")
        self.assertEqual(result, {"ok": True})

    @patch("urllib.request.urlopen")
    def test_success_diff(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"diff --git a/f b/f"
        mock_resp.headers = {}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = api_request("/test", accept="application/vnd.github.v3.diff")
        self.assertEqual(result, "diff --git a/f b/f")

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_on_502(self, mock_urlopen, mock_sleep):
        error = HTTPError("url", 502, "Bad Gateway", self._headers(), io.BytesIO(b""))
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.headers = {}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [error, mock_resp]

        result = api_request("/test")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_on_429_with_retry_after(self, mock_urlopen, mock_sleep):
        hdrs = self._headers(**{"Retry-After": "3"})
        error = HTTPError("url", 429, "Rate Limited", hdrs, io.BytesIO(b""))
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.headers = {}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [error, mock_resp]

        api_request("/test")
        mock_sleep.assert_called_with(3)

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_on_403_secondary_rate_limit(self, mock_urlopen, mock_sleep):
        hdrs = self._headers(**{"Retry-After": "5"})
        error = HTTPError(
            "url", 403, "Forbidden", hdrs, io.BytesIO(b"secondary rate limit")
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.headers = {}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [error, mock_resp]

        api_request("/test")
        mock_sleep.assert_called_with(5)

    @patch("urllib.request.urlopen")
    def test_403_without_retry_after_raises(self, mock_urlopen):
        error = HTTPError(
            "url", 403, "Forbidden", self._headers(), io.BytesIO(b"forbidden")
        )
        mock_urlopen.side_effect = error

        with self.assertRaises(ReviewError) as ctx:
            api_request("/test", max_retries=1)
        self.assertIn("403", str(ctx.exception))

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_on_network_error(self, mock_urlopen, mock_sleep):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.headers = {}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [URLError("DNS failed"), mock_resp]

        result = api_request("/test")
        self.assertEqual(result, {})

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_network_error_exhausts_retries(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = URLError("timeout")

        with self.assertRaises(ReviewError) as ctx:
            api_request("/test", max_retries=2)
        self.assertIn("Network error", str(ctx.exception))
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_404_raises_immediately(self, mock_urlopen):
        error = HTTPError(
            "url", 404, "Not Found", self._headers(), io.BytesIO(b"not found")
        )
        mock_urlopen.side_effect = error

        with self.assertRaises(ReviewError) as ctx:
            api_request("/test")
        self.assertIn("404", str(ctx.exception))
        self.assertEqual(mock_urlopen.call_count, 1)


class TestBuildSummary(unittest.TestCase):
    def _pr(self):
        return {
            "title": "Fix login",
            "number": 99,
            "user": {"login": "dev"},
            "changed_files": 3,
            "additions": 20,
            "deletions": 5,
        }

    def test_approve_no_findings(self):
        findings = {"findings": [], "looks_good": ["Clean code"]}
        summary = build_summary(findings, self._pr(), "APPROVE")
        self.assertIn("✅ Approve", summary)
        self.assertIn("Clean code", summary)
        self.assertNotIn("Blockers", summary)

    def test_request_changes_with_blocker(self):
        findings = {
            "findings": [
                {"file": "A.kt", "line": 10, "severity": "blocker", "title": "ANR risk"}
            ],
            "looks_good": ["Good naming"],
        }
        summary = build_summary(findings, self._pr(), "REQUEST_CHANGES")
        self.assertIn("🔄 Request Changes", summary)
        self.assertIn("🔴 Blockers (1)", summary)
        self.assertIn("ANR risk", summary)

    def test_groups_by_severity(self):
        findings = {
            "findings": [
                {"file": "A.kt", "line": 1, "severity": "blocker", "title": "b1"},
                {"file": "A.kt", "line": 2, "severity": "warning", "title": "w1"},
                {"file": "A.kt", "line": 3, "severity": "suggestion", "title": "s1"},
                {"file": "A.kt", "line": 4, "severity": "nit", "title": "n1"},
            ],
            "looks_good": ["ok"],
        }
        summary = build_summary(findings, self._pr(), "REQUEST_CHANGES")
        self.assertIn("🔴 Blockers (1)", summary)
        self.assertIn("🟡 Warnings (1)", summary)
        self.assertIn("💡 Suggestions (1)", summary)
        self.assertIn("🟢 Nits (1)", summary)

    def test_omits_empty_sections(self):
        findings = {
            "findings": [
                {"file": "A.kt", "line": 1, "severity": "nit", "title": "style"}
            ],
            "looks_good": ["ok"],
        }
        summary = build_summary(findings, self._pr(), "COMMENT")
        self.assertNotIn("Blockers", summary)
        self.assertNotIn("Warnings", summary)
        self.assertIn("🟢 Nits (1)", summary)

    def test_version_in_footer(self):
        findings = {"findings": [], "looks_good": ["ok"]}
        summary = build_summary(findings, self._pr(), "APPROVE")
        self.assertIn(f"v{review_pr.VERSION}", summary)


class TestVerdictLogic(unittest.TestCase):
    """Test the verdict determination logic from cmd_post (extracted)."""

    def _verdict(self, severities):
        if any(s in ("blocker", "warning") for s in severities):
            return "REQUEST_CHANGES"
        elif severities:
            return "COMMENT"
        return "APPROVE"

    def test_approve_empty(self):
        self.assertEqual(self._verdict([]), "APPROVE")

    def test_request_changes_blocker(self):
        self.assertEqual(self._verdict(["blocker"]), "REQUEST_CHANGES")

    def test_request_changes_warning(self):
        self.assertEqual(self._verdict(["warning"]), "REQUEST_CHANGES")

    def test_comment_suggestions_only(self):
        self.assertEqual(self._verdict(["suggestion", "nit"]), "COMMENT")

    def test_request_changes_mixed(self):
        self.assertEqual(
            self._verdict(["nit", "warning", "suggestion"]), "REQUEST_CHANGES"
        )


class TestIntegrationParseDiffAndSnap(unittest.TestCase):
    """End-to-end: parse a diff then snap findings to valid lines."""

    def test_snap_finding_to_diff(self):
        valid = parse_diff_lines(SAMPLE_DIFF)
        entry = valid["app/src/main/java/com/example/Foo.kt"]
        # Finding at line 11 (exact match — added line)
        line, side = find_nearest_valid_line(entry, 11)
        self.assertEqual(line, 11)
        self.assertEqual(side, "RIGHT")

    def test_snap_nearby(self):
        valid = parse_diff_lines(SAMPLE_DIFF)
        entry = valid["app/src/main/java/com/example/Foo.kt"]
        # Line 9 is not in diff, should snap to nearest valid line
        line, side = find_nearest_valid_line(entry, 9)
        self.assertIsNotNone(line)

    def test_file_not_in_diff(self):
        valid = parse_diff_lines(SAMPLE_DIFF)
        entry = valid.get("nonexistent.kt")
        line, side = find_nearest_valid_line(entry, 10)
        self.assertIsNone(line)


if __name__ == "__main__":
    unittest.main()
