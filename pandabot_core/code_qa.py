"""
pandabot_core.code_qa
~~~~~~~~~~~~~~~~~~~~
Read-only codebase querying via LLM (DeepSeek). Shallow-clones repos into a
local cache and provides search/read tools for natural language Q&A.
"""

from __future__ import annotations

import logging
import os
import subprocess
import shutil
from typing import Optional

from pandabot_core.config import cfg
from pandabot_core.llm import provider as _prov_mod

log = logging.getLogger("pandabot.code_qa")

__all__ = ["query_codebase", "list_files", "search_pattern", "read_file"]

ALLOWED_REPOS = {
    "Pandabot": "https://github.com/jcpelletier/Pandabot.git",
    "pandabot-core": "https://github.com/jcpelletier/pandabot-core.git",
    "PandabotQA": "https://github.com/jcpelletier/PandabotQA.git",
    "MediaManagement": "https://github.com/jcpelletier/MediaManagement.git",
    "space-trader": "https://github.com/jcpelletier/space-trader.git",
    "genealogy": "https://github.com/jcpelletier/genealogy.git",
}

def _get_repo_path(repo_name: str) -> str:
    """Return the absolute path to the local cache for a repo."""
    if repo_name not in ALLOWED_REPOS:
        raise ValueError(f"Repo {repo_name!r} is not in the allowed list.")
    return os.path.join(cfg.data_dir(), "repo_cache", repo_name)


def _clone_or_update(repo_name: str, timeout: int = 60) -> None:
    """Ensure the repo is cloned and up to date. Shallow clone only."""
    path = _get_repo_path(repo_name)
    url = ALLOWED_REPOS[repo_name]

    try:
        if not os.path.exists(os.path.join(path, ".git")):
            log.info("Cloning %s into %s...", repo_name, path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", url, path],
                check=True, timeout=timeout, capture_output=True, text=True
            )
        else:
            log.info("Updating %s in %s...", repo_name, path)
            subprocess.run(
                ["git", "pull"],
                cwd=path, check=True, timeout=timeout, capture_output=True, text=True
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Git operation timed out for {repo_name}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git error for {repo_name}: {e.stderr or e}")


def list_files(repo_name: str) -> list[str]:
    """Return a flat list of all file paths in the repo (excluding .git)."""
    path = _get_repo_path(repo_name)
    file_list = []
    for root, dirs, files in os.walk(path):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, path)
            file_list.append(rel_path)
    return sorted(file_list)


def search_pattern(repo_name: str, pattern: str) -> str:
    """Grep for a pattern in the repo. Returns matching lines with file:line."""
    path = _get_repo_path(repo_name)
    try:
        # -r: recursive, -n: line number, -I: skip binary, --exclude-dir=.git
        res = subprocess.run(
            ["grep", "-rnI", "--exclude-dir=.git", pattern, "."],
            cwd=path, capture_output=True, text=True, check=False
        )
        return res.stdout if res.returncode == 0 else ""
    except Exception as e:
        log.error("Grep error in %s: %s", repo_name, e)
        return ""


def read_file(repo_name: str, file_path: str, max_size_kb: int = 50) -> str:
    """Read a file from the repo. Returns content or error message."""
    base = _get_repo_path(repo_name)
    full = os.path.normpath(os.path.join(base, file_path))

    if not full.startswith(base):
        return f"Error: Path traversal attempt: {file_path}"

    if not os.path.isfile(full):
        return f"Error: File not found: {file_path}"

    size_kb = os.path.getsize(full) / 1024
    if size_kb > max_size_kb:
        return f"Error: File too large ({size_kb:.1f}KB > {max_size_kb}KB limit)"

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content
    except Exception as e:
        return f"Error reading {file_path}: {e}"


def query_codebase(question: str, repo_names: Optional[list[str]] = None) -> str:
    """Natural-language Q&A entry point. Fetches relevant files and calls DeepSeek."""
    if not repo_names:
        repo_names = list(ALLOWED_REPOS.keys())

    try:
        # 1. Update/Clone repos
        for name in repo_names:
            if name in ALLOWED_REPOS:
                _clone_or_update(name)

        # 2. Get file lists for context
        context_files = {}
        for name in repo_names:
            if name in ALLOWED_REPOS:
                context_files[name] = list_files(name)

        # 3. Use LLM to identify relevant files
        # We'll use a simple two-step approach or just feed the file list if it's small.
        # For simplicity and robust citation, we'll try to find relevant files first.
        provider = _prov_mod.get_provider("deepseek")
        model = provider.primary_model

        file_list_str = "\n".join([f"Repo {r}:\n" + "\n".join(files) for r, files in context_files.items()])

        identify_prompt = f"""Given the following list of files in the codebase, identify which files (maximum 10) are most likely to contain the information needed to answer the question: "{question}"

Files:
{file_list_str}

Respond with only a comma-separated list of "repo:path" pairs.
"""
        relevant_files_raw, _, _ = provider.complete_simple([{"role": "user", "content": identify_prompt}], model=model)

        relevant_pairs = [p.strip() for p in relevant_files_raw.split(",") if ":" in p]

        # 4. Read relevant files
        file_contents = []
        for pair in relevant_pairs[:10]:
            repo, _, path = pair.partition(":")
            if repo in ALLOWED_REPOS:
                content = read_file(repo, path)
                if not content.startswith("Error:"):
                    # Add line numbers for citation
                    lines = content.splitlines()
                    numbered = "\n".join([f"{i+1}: {line}" for i, line in enumerate(lines)])
                    file_contents.append(f"--- FILE: {repo}:{path} ---\n{numbered}")

        if not file_contents:
            return "I couldn't find any relevant files to answer your question."

        # 5. Get final answer
        all_context = "\n\n".join(file_contents)
        final_prompt = f"""You are a codebase expert. Use the following file contents to answer the question: "{question}"

Citations are MANDATORY. Every fact must cite the file and line number(s), e.g., (pandabot-core:config.py:42).

{all_context}

Answer:
"""
        answer, _, _ = provider.complete_simple([{"role": "user", "content": final_prompt}], model=model)
        return answer

    except Exception as e:
        log.exception("Error in query_codebase")
        return f"Error: {str(e)}"
