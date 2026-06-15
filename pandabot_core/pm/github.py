"""
pandabot_core.pm.github
~~~~~~~~~~~~~~~~~~~~~~~~~
GitHub Issues + Projects adapter. The panda ecosystem's project-management
backend (it replaced the OpenProject adapter).

Issues are the source of truth for work items. Projects V2 boards are the human
planning overlay and are populated automatically, so this adapter intentionally
covers only Issues, sub-issues, milestones, and search — the operations the bots
actually need. Board reads/writes (GraphQL `projectsV2`) are left to the `github`
Claude Code skill, which has the `project` token scope.

Auth: a token in GITHUB_TOKEN, sent as `Authorization: Bearer <token>`. A
fine-grained PAT with Issues read/write (or a classic token with `repo`) is
enough — no `project` scope required.

All public functions return JSON strings or human-readable error strings — they
never raise, so the LLM always gets a readable result.

Env vars:
    ENABLE_GITHUB_PM    feature flag (default false)
    GITHUB_TOKEN        token with Issues read/write on the repos
    GITHUB_OWNER        default owner for short repo names (default 'jcpelletier')
    GITHUB_REPOS        optional comma-separated allowlist of repos for list_repos
"""

from __future__ import annotations

import json
import logging
import os

import requests

log = logging.getLogger("pandabot.pm.github")

_API = "https://api.github.com"

__all__ = [
    "list_repos", "get_repo", "list_issues", "get_issue",
    "list_sub_issues", "search_issues",
    "list_milestones", "list_milestone_issues",
    "create_issue", "update_issue",
    "add_comment", "list_comments", "set_status_label", "list_children_with_status",
]


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _owner() -> str:
    return os.environ.get("GITHUB_OWNER", "jcpelletier").strip()


def _enabled() -> bool:
    return os.environ.get("ENABLE_GITHUB_PM", "false").lower() == "true"


def _repos_allowlist() -> list[str]:
    raw = os.environ.get("GITHUB_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _full(repo: str) -> str:
    """Resolve a short repo name to 'owner/name' (pass-through if already full)."""
    repo = repo.strip()
    return repo if "/" in repo else f"{_owner()}/{repo}"


def _gh(method: str, path: str, **kwargs) -> object:
    url = path if path.startswith("http") else f"{_API}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _token():
        headers["Authorization"] = f"Bearer {_token()}"
    headers.update(kwargs.pop("headers", {}))
    r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else {}


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _slim_issue(it: dict) -> dict:
    return {
        "number":      it.get("number"),
        "id":          it.get("id"),
        "title":       it.get("title"),
        "state":       it.get("state"),
        "labels":      [l.get("name") for l in it.get("labels", []) if isinstance(l, dict)],
        "assignees":   [a.get("login") for a in it.get("assignees", [])],
        "milestone":   (it.get("milestone") or {}).get("title"),
        "body":        it.get("body") or "",
        "url":         it.get("html_url"),
        "is_pull_request": "pull_request" in it,
        "created_at":  it.get("created_at"),
        "updated_at":  it.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_repos() -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    repos = _repos_allowlist()
    if not repos:
        return ("No repos configured. Set GITHUB_REPOS to a comma-separated list "
                "(short names resolve against GITHUB_OWNER).")
    return json.dumps([{"repo": _full(r)} for r in repos], indent=2)


def get_repo(repo: str) -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        data = _gh("GET", f"/repos/{_full(repo)}")
        return json.dumps({
            "full_name":   data.get("full_name"),
            "description": data.get("description"),
            "private":     data.get("private"),
            "open_issues": data.get("open_issues_count"),
            "url":         data.get("html_url"),
        }, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def list_issues(repo: str, state: str = "open", limit: int = 25) -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        state = state if state in ("open", "closed", "all") else "open"
        params = {"state": state, "per_page": min(int(limit), 100), "sort": "updated", "direction": "desc"}
        data = _gh("GET", f"/repos/{_full(repo)}/issues", params=params)
        # The issues endpoint also returns PRs — drop them.
        issues = [_slim_issue(it) for it in data if "pull_request" not in it]
        return json.dumps({"repo": _full(repo), "count": len(issues), "issues": issues}, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def get_issue(repo: str, number: int) -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        return json.dumps(_slim_issue(_gh("GET", f"/repos/{_full(repo)}/issues/{number}")), indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def list_sub_issues(repo: str, number: int) -> str:
    """List child (sub-)issues of an issue — the GitHub analogue of epic children."""
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        data = _gh("GET", f"/repos/{_full(repo)}/issues/{number}/sub_issues")
        children = [_slim_issue(it) for it in data]
        return json.dumps({"repo": _full(repo), "parent": number, "count": len(children), "sub_issues": children}, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def search_issues(query: str, repo: str = "", limit: int = 25) -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        q = f"{query} is:issue"
        if repo:
            q += f" repo:{_full(repo)}"
        else:
            q += f" user:{_owner()}"
        data = _gh("GET", "/search/issues", params={"q": q, "per_page": min(int(limit), 100)})
        items = [_slim_issue(it) for it in data.get("items", [])]
        return json.dumps({"query": query, "total": data.get("total_count", len(items)),
                           "shown": len(items), "issues": items}, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def list_milestones(repo: str, state: str = "open") -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        data = _gh("GET", f"/repos/{_full(repo)}/milestones", params={"state": state})
        ms = [{"number": m.get("number"), "title": m.get("title"), "state": m.get("state"),
               "due_on": m.get("due_on"), "open_issues": m.get("open_issues"),
               "closed_issues": m.get("closed_issues")} for m in data]
        return json.dumps(ms, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def list_milestone_issues(repo: str, milestone: int, state: str = "all") -> str:
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        params = {"milestone": milestone, "state": state, "per_page": 100}
        data = _gh("GET", f"/repos/{_full(repo)}/issues", params=params)
        issues = [_slim_issue(it) for it in data if "pull_request" not in it]
        return json.dumps({"repo": _full(repo), "milestone": milestone, "count": len(issues),
                           "issues": issues}, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def create_issue(repo: str, title: str, body: str = "", labels: str = "",
                 assignee: str = "", milestone: int = 0, parent: int = 0) -> str:
    """Create an issue. labels is comma-separated. If parent>0, link the new
    issue as a sub-issue of that parent (epic -> child)."""
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        full = _full(repo)
        payload: dict = {"title": title}
        if body:
            payload["body"] = body
        if labels:
            payload["labels"] = _csv(labels)
        if assignee:
            payload["assignees"] = [assignee]
        if milestone:
            payload["milestone"] = int(milestone)
        created = _gh("POST", f"/repos/{full}/issues", json=payload)
        result = _slim_issue(created)
        if parent:
            try:
                _gh("POST", f"/repos/{full}/issues/{parent}/sub_issues",
                    json={"sub_issue_id": created["id"]})
                result["parent"] = parent
            except Exception as link_err:
                result["parent_link_error"] = str(link_err)
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def add_comment(repo: str, number: int, body: str) -> str:
    """Post a comment on an issue. This is the cross-bot rendezvous for progress
    notes and QA verdicts — a durable, auditable record on the work item itself."""
    if not _enabled():
        return "GitHub integration is not enabled."
    if not body.strip():
        return "Nothing to post: comment body is empty."
    try:
        created = _gh("POST", f"/repos/{_full(repo)}/issues/{number}/comments",
                      json={"body": body})
        return json.dumps({
            "repo": _full(repo), "issue": number,
            "comment_id": created.get("id"), "url": created.get("html_url"),
        }, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def list_comments(repo: str, number: int, limit: int = 10) -> str:
    """Return the most recent comments on an issue (oldest-first within the page).
    The goal driver uses this to read PandaQA's verdict/gap-list back off a story."""
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        params = {"per_page": min(int(limit), 100), "sort": "created", "direction": "desc"}
        data = _gh("GET", f"/repos/{_full(repo)}/issues/{number}/comments", params=params)
        comments = [{
            "id": c.get("id"),
            "user": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"),
            "body": c.get("body") or "",
        } for c in (data if isinstance(data, list) else [])]
        return json.dumps({"repo": _full(repo), "issue": number,
                           "count": len(comments), "comments": comments}, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def set_status_label(repo: str, number: int, status: str) -> str:
    """Swap the issue's ``status: *`` label for ``status`` (a full label string
    like ``status: in-qa``), leaving every other label intact. The status-label
    state machine that drives the goal lifecycle relies on exactly one status
    label per issue."""
    if not _enabled():
        return "GitHub integration is not enabled."
    status = status.strip()
    if not status:
        return "Nothing to set: status label is empty."
    try:
        full = _full(repo)
        issue = _gh("GET", f"/repos/{full}/issues/{number}")
        current = [l.get("name") for l in issue.get("labels", []) if isinstance(l, dict)]
        kept = [name for name in current if name and not name.lower().startswith("status:")]
        new_labels = kept + [status]
        updated = _gh("PATCH", f"/repos/{full}/issues/{number}",
                      json={"labels": new_labels})
        return json.dumps(_slim_issue(updated), indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def list_children_with_status(repo: str, number: int) -> str:
    """List an epic's sub-issues with just the fields the goal driver needs to
    pick the next actionable story: number, title, state, and its ``status:`` /
    ``type:`` labels. Thin projection over :func:`list_sub_issues`."""
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        data = _gh("GET", f"/repos/{_full(repo)}/issues/{number}/sub_issues")
        children = []
        for it in data:
            labels = [l.get("name") for l in it.get("labels", []) if isinstance(l, dict)]
            status = next((n for n in labels if n and n.lower().startswith("status:")), None)
            type_ = next((n for n in labels if n and n.lower().startswith("type:")), None)
            children.append({
                "number": it.get("number"),
                "title": it.get("title"),
                "state": it.get("state"),
                "status": status,
                "type": type_,
                "url": it.get("html_url"),
            })
        return json.dumps({"repo": _full(repo), "parent": number,
                           "count": len(children), "children": children}, indent=2)
    except Exception as e:
        return f"GitHub error: {e}"


def update_issue(repo: str, number: int, title: str = "", body: str = "",
                 state: str = "", labels: str = "", assignee: str = "",
                 milestone: int = 0) -> str:
    """Update fields on an issue. Only supplied fields change. state is 'open' or
    'closed'. Pass milestone=-1 to clear the milestone."""
    if not _enabled():
        return "GitHub integration is not enabled."
    try:
        payload: dict = {}
        if title:
            payload["title"] = title
        if body:
            payload["body"] = body
        if state in ("open", "closed"):
            payload["state"] = state
        if labels:
            payload["labels"] = _csv(labels)
        if assignee:
            payload["assignees"] = [assignee]
        if milestone == -1:
            payload["milestone"] = None
        elif milestone:
            payload["milestone"] = int(milestone)
        if not payload:
            return "Nothing to update: supply at least one field."
        updated = _gh("PATCH", f"/repos/{_full(repo)}/issues/{number}", json=payload)
        return json.dumps(_slim_issue(updated), indent=2)
    except Exception as e:
        return f"GitHub error: {e}"
