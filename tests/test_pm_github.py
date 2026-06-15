"""Tests for pandabot_core.pm.github — the GitHub Issues adapter."""
import json

import pytest

import pandabot_core.pm.github as gh


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = b"x" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Calls(list):
    """A list of recorded requests with a .queue of canned responses."""
    queue: list


@pytest.fixture
def calls(monkeypatch):
    """Capture outbound requests and return canned JSON per call."""
    recorded = _Calls()
    recorded.queue = []

    def fake_request(method, url, **kwargs):
        recorded.append({"method": method, "url": url, **kwargs})
        payload = recorded.queue.pop(0) if recorded.queue else {}
        return _FakeResponse(payload)

    monkeypatch.setattr(gh.requests, "request", fake_request)
    return recorded


@pytest.fixture(autouse=True)
def enabled_env(monkeypatch):
    monkeypatch.setenv("ENABLE_GITHUB_PM", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_OWNER", "jcpelletier")
    monkeypatch.delenv("GITHUB_REPOS", raising=False)


def test_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv("ENABLE_GITHUB_PM", "false")
    assert gh.list_issues("Pandabot") == "GitHub integration is not enabled."
    assert gh.create_issue("Pandabot", "x") == "GitHub integration is not enabled."


def test_full_resolves_short_name():
    assert gh._full("Pandabot") == "jcpelletier/Pandabot"
    assert gh._full("owner/Repo") == "owner/Repo"


def test_list_issues_drops_prs_and_slims(calls):
    calls.queue.append([
        {"number": 1, "title": "real issue", "state": "open", "labels": [{"name": "type: bug"}]},
        {"number": 2, "title": "a PR", "state": "open", "pull_request": {"url": "x"}},
    ])
    out = json.loads(gh.list_issues("Pandabot", state="open", limit=10))
    assert out["repo"] == "jcpelletier/Pandabot"
    assert out["count"] == 1
    assert out["issues"][0]["number"] == 1
    assert out["issues"][0]["labels"] == ["type: bug"]
    # URL + params went to the right place
    assert calls[0]["url"].endswith("/repos/jcpelletier/Pandabot/issues")
    assert calls[0]["params"]["state"] == "open"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_create_issue_splits_labels_and_links_parent(calls):
    calls.queue.append({"number": 99, "id": 555, "title": "child", "state": "open"})  # create
    calls.queue.append({})  # sub_issue link
    out = json.loads(gh.create_issue("Pandabot", "child", body="b",
                                     labels="type: story, status: new", parent=98))
    assert out["number"] == 99
    assert out["parent"] == 98
    create_call = calls[0]
    assert create_call["method"] == "POST"
    assert create_call["json"]["labels"] == ["type: story", "status: new"]
    link_call = calls[1]
    assert link_call["url"].endswith("/repos/jcpelletier/Pandabot/issues/98/sub_issues")
    assert link_call["json"]["sub_issue_id"] == 555


def test_update_issue_requires_a_field():
    assert gh.update_issue("Pandabot", 1) == "Nothing to update: supply at least one field."


def test_update_issue_sends_state(calls):
    calls.queue.append({"number": 1, "title": "t", "state": "closed"})
    out = json.loads(gh.update_issue("Pandabot", 1, state="closed"))
    assert out["state"] == "closed"
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["json"] == {"state": "closed"}


def test_search_scopes_to_repo(calls):
    calls.queue.append({"total_count": 1, "items": [{"number": 5, "title": "voice latency", "state": "open"}]})
    out = json.loads(gh.search_issues("latency", repo="Pandabot"))
    assert out["total"] == 1
    q = calls[0]["params"]["q"]
    assert "is:issue" in q
    assert "repo:jcpelletier/Pandabot" in q


def test_list_repos_uses_allowlist(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", "Pandabot, pandabot-core")
    out = json.loads(gh.list_repos())
    assert {r["repo"] for r in out} == {"jcpelletier/Pandabot", "jcpelletier/pandabot-core"}


def test_add_comment_posts_body(calls):
    calls.queue.append({"id": 7, "html_url": "https://github.com/o/r/issues/3#c7"})
    out = json.loads(gh.add_comment("Pandabot", 3, "qa-passed: all criteria met"))
    assert out["comment_id"] == 7
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/repos/jcpelletier/Pandabot/issues/3/comments")
    assert calls[0]["json"]["body"] == "qa-passed: all criteria met"


def test_add_comment_rejects_empty():
    assert gh.add_comment("Pandabot", 3, "   ") == "Nothing to post: comment body is empty."


def test_set_status_label_swaps_only_status(calls):
    # GET current labels, then PATCH the new label set.
    calls.queue.append({"number": 5, "labels": [
        {"name": "type: story"}, {"name": "status: in-progress"}, {"name": "area: ui"},
    ]})
    calls.queue.append({"number": 5, "state": "open",
                        "labels": [{"name": "type: story"}, {"name": "area: ui"},
                                   {"name": "status: in-qa"}]})
    out = json.loads(gh.set_status_label("game", 5, "status: in-qa"))
    assert "status: in-qa" in out["labels"]
    patch = calls[1]
    assert patch["method"] == "PATCH"
    sent = patch["json"]["labels"]
    assert "status: in-progress" not in sent       # old status dropped
    assert "type: story" in sent and "area: ui" in sent  # others kept
    assert "status: in-qa" in sent


def test_list_comments_slims(calls):
    calls.queue.append([
        {"id": 1, "user": {"login": "pandaqa"}, "created_at": "t", "body": "changes-requested: missing win condition"},
    ])
    out = json.loads(gh.list_comments("game", 5, limit=5))
    assert out["count"] == 1
    assert out["comments"][0]["user"] == "pandaqa"
    assert "missing win condition" in out["comments"][0]["body"]
    assert calls[0]["url"].endswith("/repos/jcpelletier/game/issues/5/comments")


def test_list_children_with_status_projects_labels(calls):
    calls.queue.append([
        {"number": 10, "title": "story A", "state": "open",
         "labels": [{"name": "type: story"}, {"name": "status: ready"}]},
        {"number": 11, "title": "story B", "state": "closed",
         "labels": [{"name": "status: done"}]},
    ])
    out = json.loads(gh.list_children_with_status("game", 1))
    assert out["count"] == 2
    assert out["children"][0]["status"] == "status: ready"
    assert out["children"][0]["type"] == "type: story"
    assert out["children"][1]["status"] == "status: done"
