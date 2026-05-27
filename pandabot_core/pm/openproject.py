"""
pandabot_core.pm.openproject
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
OpenProject v3 REST API adapter.

Auth: HTTP Basic with "apikey" as username and the token as password.
All public functions return JSON strings or human-readable error strings —
they never raise so Claude always gets a readable result.

Env vars:
    ENABLE_OPENPROJECT          feature flag (default false)
    OPENPROJECT_URL             base URL, e.g. https://plan.jpelletier.com
    OPENPROJECT_API_KEY         API key for the bot's OpenProject account
    OPENPROJECT_DEFAULT_PROJECT optional default project identifier
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse

import requests

log = logging.getLogger("pandabot.pm.openproject")

__all__ = [
    "list_projects", "get_project", "list_work_packages", "get_work_package",
    "list_children", "list_versions", "list_version_tickets", "search_work_packages",
    "list_project_members", "list_types",
    "create_project", "set_project_parent",
    "create_work_package", "update_work_package",
    "add_project_member", "remove_project_member",
]


def _url() -> str:
    return os.environ.get("OPENPROJECT_URL", "").rstrip("/")


def _key() -> str:
    return os.environ.get("OPENPROJECT_API_KEY", "")


def _enabled() -> bool:
    return os.environ.get("ENABLE_OPENPROJECT", "false").lower() == "true"


def _op(method: str, path: str, **kwargs) -> dict:
    url = f"{_url()}/api/v3{path}"
    r = requests.request(method, url, auth=("apikey", _key()),
                         headers={"Content-Type": "application/json"}, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else {}


def _slim_wp(wp: dict) -> dict:
    lnk = wp.get("_links", {})
    return {
        "id":          wp.get("id"),
        "subject":     wp.get("subject"),
        "type":        lnk.get("type", {}).get("title"),
        "status":      lnk.get("status", {}).get("title"),
        "priority":    lnk.get("priority", {}).get("title"),
        "assignee":    lnk.get("assignee", {}).get("title"),
        "version":     lnk.get("version", {}).get("title"),
        "project":     lnk.get("project", {}).get("title"),
        "description": (wp.get("description") or {}).get("raw", ""),
        "due_date":    wp.get("dueDate"),
        "start_date":  wp.get("startDate"),
        "created_at":  wp.get("createdAt"),
        "updated_at":  wp.get("updatedAt"),
    }


def _slim_project(p: dict) -> dict:
    return {
        "id":          p.get("id"),
        "identifier":  p.get("identifier"),
        "name":        p.get("name"),
        "description": (p.get("description") or {}).get("raw", ""),
        "active":      p.get("active", True),
        "created_at":  p.get("createdAt"),
    }


def _find_user(login_or_email: str) -> dict | None:
    for field in ("login", "email"):
        filters = urllib.parse.quote(json.dumps([{field: {"operator": "=", "values": [login_or_email]}}]))
        elements = _op("GET", f"/users?filters={filters}").get("_embedded", {}).get("elements", [])
        if elements:
            return elements[0]
    return None


def _find_role(name: str) -> dict | None:
    for r in _op("GET", "/roles").get("_embedded", {}).get("elements", []):
        if r.get("name", "").lower() == name.lower():
            return r
    return None


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_projects() -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        data = _op("GET", "/projects?pageSize=100")
        projects = [_slim_project(p) for p in data.get("_embedded", {}).get("elements", [])]
        return json.dumps(projects, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def get_project(project: str) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        return json.dumps(_slim_project(_op("GET", f"/projects/{project}")), indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def list_work_packages(project: str, status: str = "open", limit: int = 25) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        if status == "open":
            filters = json.dumps([{"status": {"operator": "o", "values": []}}])
        elif status == "closed":
            filters = json.dumps([{"status": {"operator": "c", "values": []}}])
        else:
            filters = json.dumps([])
        params = (
            f"?filters={urllib.parse.quote(filters)}"
            f"&pageSize={limit}"
            f"&sortBy=%5B%5B%22updatedAt%22%2C%22desc%22%5D%5D"
        )
        data = _op("GET", f"/projects/{project}/work_packages{params}")
        wps = [_slim_wp(wp) for wp in data.get("_embedded", {}).get("elements", [])]
        return json.dumps({"total": data.get("total", len(wps)), "shown": len(wps), "work_packages": wps}, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def get_work_package(wp_id: int) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        return json.dumps(_slim_wp(_op("GET", f"/work_packages/{wp_id}")), indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def list_children(parent_wp_id: int) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        filters = urllib.parse.quote(
            json.dumps([{"parent": {"operator": "=", "values": [str(parent_wp_id)]}}])
        )
        data = _op("GET", f"/work_packages?filters={filters}&pageSize=100")
        wps = [_slim_wp(wp) for wp in data.get("_embedded", {}).get("elements", [])]
        return json.dumps({"parent_wp_id": parent_wp_id, "count": len(wps), "work_packages": wps}, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def list_versions(project: str) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        data = _op("GET", f"/projects/{project}/versions")
        versions = [
            {"id": v.get("id"), "name": v.get("name"), "status": v.get("status"),
             "start_date": v.get("startDate"), "end_date": v.get("endDate")}
            for v in data.get("_embedded", {}).get("elements", [])
        ]
        return json.dumps(versions, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def list_version_tickets(version_id: int) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        filters = urllib.parse.quote(json.dumps([{"version": {"operator": "=", "values": [str(version_id)]}}]))
        data = _op("GET", f"/work_packages?filters={filters}&pageSize=100")
        wps = [_slim_wp(wp) for wp in data.get("_embedded", {}).get("elements", [])]
        return json.dumps({"version_id": version_id, "count": len(wps), "work_packages": wps}, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def search_work_packages(query: str, project: str = "", limit: int = 25) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        filters = urllib.parse.quote(json.dumps([{"subjectOrId": {"operator": "**", "values": [query]}}]))
        path = f"/projects/{project}/work_packages" if project else "/work_packages"
        data = _op("GET", f"{path}?filters={filters}&pageSize={limit}")
        wps = [_slim_wp(wp) for wp in data.get("_embedded", {}).get("elements", [])]
        return json.dumps({"query": query, "total": data.get("total", len(wps)), "shown": len(wps), "work_packages": wps}, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def list_project_members(project: str) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        data = _op("GET", f"/projects/{project}/members?pageSize=100")
        members = [
            {"id": m.get("id"),
             "name": m.get("_links", {}).get("principal", {}).get("title"),
             "roles": [r.get("title") for r in m.get("_links", {}).get("roles", [])]}
            for m in data.get("_embedded", {}).get("elements", [])
        ]
        return json.dumps(members, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def list_types(project: str = "") -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        path = f"/projects/{project}/types" if project else "/types"
        data = _op("GET", path)
        types = [{"id": t.get("id"), "name": t.get("name")}
                 for t in data.get("_embedded", {}).get("elements", [])]
        return json.dumps(types, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


# ---------------------------------------------------------------------------
# Write operations (require elevated permissions)
# ---------------------------------------------------------------------------

def create_project(name: str, identifier: str, description: str = "", parent: str = "") -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        body: dict = {"name": name, "identifier": identifier}
        if description:
            body["description"] = {"format": "markdown", "raw": description}
        if parent:
            body["_links"] = {"parent": {"href": f"/api/v3/projects/{parent}"}}
        return json.dumps(_slim_project(_op("POST", "/projects", json=body)), indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def set_project_parent(project: str, parent: str) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        if parent in ("", "none", "null"):
            body = {"_links": {"parent": {"href": None}}}
        else:
            body = {"_links": {"parent": {"href": f"/api/v3/projects/{parent}"}}}
        return json.dumps(_slim_project(_op("PATCH", f"/projects/{project}", json=body)), indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def create_work_package(
    project: str, subject: str, type_id: int = 1,
    description: str = "", assignee: str = "",
    start_date: str = "", due_date: str = "", parent_wp_id: int = 0,
) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        body: dict = {"subject": subject, "_links": {"type": {"href": f"/api/v3/types/{type_id}"}}}
        if description:
            body["description"] = {"format": "markdown", "raw": description}
        if start_date:
            body["startDate"] = start_date
        if due_date:
            body["dueDate"] = due_date
        if assignee:
            user = _find_user(assignee)
            if not user:
                return f"User not found: {assignee!r}"
            body["_links"]["assignee"] = {"href": user["_links"]["self"]["href"]}
        if parent_wp_id:
            body["_links"]["parent"] = {"href": f"/api/v3/work_packages/{parent_wp_id}"}
        return json.dumps(_slim_wp(_op("POST", f"/projects/{project}/work_packages", json=body)), indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def update_work_package(
    wp_id: int, subject: str = "", type_id: int = 0,
    description: str = "", assignee: str = "", status: str = "",
    start_date: str = "", due_date: str = "", parent_wp_id: int = 0,
) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        current = _op("GET", f"/work_packages/{wp_id}")
        lock_version = current.get("lockVersion", 0)
        body: dict = {"lockVersion": lock_version, "_links": {}}
        if subject:
            body["subject"] = subject
        if description:
            body["description"] = {"format": "markdown", "raw": description}
        if start_date:
            body["startDate"] = start_date
        if due_date:
            body["dueDate"] = due_date
        if type_id:
            body["_links"]["type"] = {"href": f"/api/v3/types/{type_id}"}
        if assignee:
            user = _find_user(assignee)
            if not user:
                return f"User not found: {assignee!r}"
            body["_links"]["assignee"] = {"href": user["_links"]["self"]["href"]}
        if status:
            statuses = _op("GET", "/statuses").get("_embedded", {}).get("elements", [])
            match = next((s for s in statuses if s.get("name", "").lower() == status.lower()), None)
            if not match:
                names = [s.get("name") for s in statuses]
                return f"Status not found: {status!r}. Available: {names}"
            body["_links"]["status"] = {"href": match["_links"]["self"]["href"]}
        if parent_wp_id == -1:
            body["_links"]["parent"] = {"href": None}
        elif parent_wp_id > 0:
            body["_links"]["parent"] = {"href": f"/api/v3/work_packages/{parent_wp_id}"}
        return json.dumps(_slim_wp(_op("PATCH", f"/work_packages/{wp_id}", json=body)), indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def add_project_member(project: str, user: str, role: str) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        user_obj = _find_user(user)
        if not user_obj:
            return f"User not found: {user!r}"
        role_obj = _find_role(role)
        if not role_obj:
            names = [r.get("name") for r in _op("GET", "/roles").get("_embedded", {}).get("elements", [])]
            return f"Role not found: {role!r}. Available: {names}"
        body = {"_links": {
            "project":   {"href": _op("GET", f"/projects/{project}")["_links"]["self"]["href"]},
            "principal": {"href": user_obj["_links"]["self"]["href"]},
            "roles":     [{"href": role_obj["_links"]["self"]["href"]}],
        }}
        result = _op("POST", "/memberships", json=body)
        lnk = result.get("_links", {})
        return json.dumps({
            "membership_id": result.get("id"),
            "user":    lnk.get("principal", {}).get("title"),
            "project": lnk.get("project", {}).get("title"),
            "roles":   [r.get("title") for r in lnk.get("roles", [])],
        }, indent=2)
    except Exception as e:
        return f"OpenProject error: {e}"


def remove_project_member(membership_id: int) -> str:
    if not _enabled():
        return "OpenProject integration is not enabled."
    try:
        _op("DELETE", f"/memberships/{membership_id}")
        return f"Membership {membership_id} removed."
    except Exception as e:
        return f"OpenProject error: {e}"
