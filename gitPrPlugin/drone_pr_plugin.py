#!/usr/bin/env python3
"""
Drone plugin: clone a repo, append a marker comment to each YAML under a path, push
one branch per file, then open a pull request per branch (Harness Code, GitHub, or
Bitbucket Cloud REST API).
"""

import base64
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse
import shutil


class PluginError(Exception):
    pass


DEFAULT_YAML_COMMENT = (
    "# Remove this comment post your chnges are done , "
    "this was created as part of auto creation of PR for easier view"
)


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("drone_pr_plugin")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        log.addHandler(h)
    return log


logger = _setup_logger()


def run_git(args: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def authenticated_clone_url(repo_url: str, username: str, token: str) -> str:
    """Embed HTTPS credentials when provided (same pattern as drone_git_plugin.py)."""
    if repo_url.startswith("https://") and username and token:
        return repo_url.replace("https://", f"https://{username}:{token}@", 1)
    return repo_url


def sanitize_repo_url_for_output(repo_url: str) -> str:
    """Strip embedded credentials for logging / DRONE_OUTPUT."""
    if "@" in repo_url and "://" in repo_url:
        return "https://" + repo_url.split("@", 1)[-1]
    return repo_url


def append_comment_line(text: str, comment_line: str) -> str:
    """Append exactly one marker line at the bottom (YAML # comment)."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + comment_line.strip() + "\n"


def slug_for_branch(name: str) -> str:
    """Git branch segment: alphanumeric, dot, underscore, hyphen."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip())
    return s.strip("-") or "service"


def discover_yaml_files(root: Path, relative_dir: str) -> List[Path]:
    base = root / relative_dir.strip("/ ")
    if not base.is_dir():
        raise PluginError(f"Harness path is not a directory: {relative_dir}")
    files: List[Path] = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".yaml", ".yml"):
            files.append(p)
    return files


def parse_github_repo(repo_url: str) -> Tuple[str, str]:
    """Return (owner, repo) from GitHub-style https or git@ URL."""
    u = repo_url.strip().rstrip("/")
    if u.startswith("git@"):
        m = re.search(r"git@[^:]+:([^/]+)/([^/]+?)(?:\.git)?$", u)
        if m:
            return m.group(1), m.group(2)
        raise PluginError(f"Could not parse owner/repo from PLUGIN_REPO_URL: {repo_url}")
    if "://" in u and "@" in u:
        u = re.sub(r"https?://[^@]+@", "https://", u)
    parsed = urlparse(u)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1].replace(".git", "")
    raise PluginError(f"Could not parse owner/repo from PLUGIN_REPO_URL: {repo_url}")


def parse_bitbucket_cloud_repo(repo_url: str) -> Tuple[str, str]:
    """Return (workspace, repo_slug) from bitbucket.org https or git@ URL."""
    u = repo_url.strip().rstrip("/")
    if u.startswith("git@"):
        m = re.search(
            r"git@bitbucket\.org:([^/]+)/([^/]+?)(?:\.git)?$",
            u,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1), m.group(2)
        raise PluginError(
            f"Could not parse workspace/repo_slug from PLUGIN_REPO_URL: {repo_url}"
        )
    if "://" in u and "@" in u:
        u = re.sub(r"https?://[^@]+@", "https://", u)
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if "bitbucket.org" not in host:
        raise PluginError(
            "Could not parse Bitbucket workspace/repo_slug from PLUGIN_REPO_URL "
            f"(not bitbucket.org): {repo_url}"
        )
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1].replace(".git", "")
    raise PluginError(
        f"Could not parse workspace/repo_slug from PLUGIN_REPO_URL: {repo_url}"
    )


def resolve_bitbucket_cloud_workspace_repo(cfg: Dict[str, Any]) -> Tuple[str, str]:
    ws = str(cfg.get("bitbucket_workspace") or "").strip()
    slug = str(cfg.get("bitbucket_repo_slug") or "").strip()
    if ws and slug:
        return ws, slug
    return parse_bitbucket_cloud_repo(str(cfg.get("repo_url") or ""))


def bitbucket_cloud_auth_headers(
    username: str,
    app_password: str,
    bearer_token: str,
) -> Dict[str, str]:
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}"}
    if username and app_password:
        raw = f"{username}:{app_password}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    raise PluginError(
        "Bitbucket PR: set PLUGIN_BITBUCKET_ACCESS_TOKEN (OAuth2), or "
        "PLUGIN_BITBUCKET_USERNAME + PLUGIN_BITBUCKET_APP_PASSWORD — "
        "or use PLUGIN_GIT_USERNAME + PLUGIN_GIT_TOKEN (app password) for Basic auth"
    )


def bitbucket_cloud_pr_html_url(pr: Dict[str, Any]) -> str:
    links = pr.get("links") or {}
    html = links.get("html")
    if isinstance(html, dict):
        return str(html.get("href") or "")
    if isinstance(html, list) and html and isinstance(html[0], dict):
        return str(html[0].get("href") or "")
    return ""


def create_pull_request_bitbucket_cloud(
    api_base: str,
    workspace: str,
    repo_slug: str,
    title: str,
    source_branch: str,
    dest_branch: str,
    description: str,
    username: str,
    app_password: str,
    bearer_token: str,
) -> Dict[str, Any]:
    """Bitbucket Cloud REST API 2.0."""
    w = quote(workspace, safe="")
    r = quote(repo_slug, safe="")
    url = f"{api_base.rstrip('/')}/repositories/{w}/{r}/pullrequests"
    headers = {
        **bitbucket_cloud_auth_headers(username, app_password, bearer_token),
    }
    _, result = http_json(
        "POST",
        url,
        headers,
        {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": dest_branch}},
        },
    )
    return result if isinstance(result, dict) else {}


def http_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 120,
) -> Tuple[int, Any]:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if raw:
                return resp.status, json.loads(raw)
            return resp.status, None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body) if err_body else {}
        except json.JSONDecodeError:
            parsed = err_body
        raise PluginError(f"HTTP {e.code} {method} {url}: {parsed}") from e


def create_pull_request_github(
    api_url: str,
    token: str,
    owner: str,
    repo: str,
    title: str,
    head_branch: str,
    base_branch: str,
    description: str,
) -> Dict[str, Any]:
    path = f"/repos/{owner}/{repo}/pulls"
    url = api_url.rstrip("/") + path
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    _, result = http_json(
        "POST",
        url,
        headers,
        {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": description,
        },
    )
    return result if isinstance(result, dict) else {}


def create_pull_request_harness(
    platform_url: str,
    api_key: str,
    repo_identifier: str,
    account_identifier: str,
    org_identifier: str,
    project_identifier: str,
    title: str,
    source_branch: str,
    target_branch: str,
    description: str,
) -> Dict[str, Any]:
    base = platform_url.rstrip("/") + "/code/api/v1/repos/"
    path_repo = quote(repo_identifier, safe="/")
    query: Dict[str, str] = {"accountIdentifier": account_identifier}
    if org_identifier:
        query["orgIdentifier"] = org_identifier
    if project_identifier:
        query["projectIdentifier"] = project_identifier
    qs = urlencode(query)
    url = f"{base}{path_repo}/pullreq?{qs}"
    headers = {"x-api-key": api_key}
    body = {
        "title": title,
        "description": description,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "is_draft": False,
    }
    _, result = http_json("POST", url, headers, body)
    return result if isinstance(result, dict) else {}


def resolve_pr_backend(cfg: Dict[str, Any]) -> str:
    explicit = str(cfg.get("pr_backend") or "").strip().lower()
    if explicit in ("none", "off", "false", "0"):
        return "none"
    if explicit == "github":
        return "github"
    if explicit == "harness":
        return "harness"
    if explicit == "bitbucket":
        return "bitbucket"
    # Auto-detect when PLUGIN_PR_BACKEND is unset
    harness_ready = (
        cfg.get("harness_api_key")
        and cfg.get("harness_repo_identifier")
        and cfg.get("harness_account_identifier")
    )
    if harness_ready:
        return "harness"
    repo = str(cfg.get("repo_url") or "").lower()
    if "github.com" in repo:
        return "github"
    if "bitbucket.org" in repo:
        return "bitbucket"
    raise PluginError(
        "Cannot determine PR backend: set PLUGIN_PR_BACKEND to harness, github, "
        "bitbucket, or none. "
        "Harness: PLUGIN_HARNESS_API_KEY, PLUGIN_HARNESS_REPO_IDENTIFIER, "
        "PLUGIN_HARNESS_ACCOUNT_IDENTIFIER. GitHub: github.com in URL and "
        "PLUGIN_GITHUB_TOKEN or PLUGIN_GIT_TOKEN. Bitbucket Cloud: bitbucket.org "
        "in URL (or set PLUGIN_BITBUCKET_WORKSPACE + PLUGIN_BITBUCKET_REPO_SLUG with "
        "PLUGIN_PR_BACKEND=bitbucket) and OAuth or app password credentials."
    )


def load_config() -> Dict[str, Any]:
    repo_url = os.environ.get("PLUGIN_REPO_URL", "").strip()
    ticket = os.environ.get("PLUGIN_CHANGE_TICKET", "").strip()
    harness_path = os.environ.get("PLUGIN_HARNESS_PATH", ".harness").strip() or ".harness"
    base_branch = os.environ.get("PLUGIN_BASE_BRANCH", "main").strip() or "main"
    branch_prefix = os.environ.get("PLUGIN_BRANCH_PREFIX", "release").strip() or "release"
    work_dir = os.environ.get("PLUGIN_WORK_DIR", "/harness").strip() or "/harness"
    username = os.environ.get("PLUGIN_GIT_USERNAME", "").strip()
    token = os.environ.get("PLUGIN_GIT_TOKEN", "").strip()
    git_name = os.environ.get("PLUGIN_GIT_AUTHOR_NAME", "Drone PR Plugin").strip()
    git_email = os.environ.get("PLUGIN_GIT_AUTHOR_EMAIL", "drone-pr-plugin@local").strip()
    title_tmpl = os.environ.get(
        "PLUGIN_PR_TITLE_TEMPLATE",
        "[{ticket}] {service} harness update",
    ).strip()
    comment_line = os.environ.get("PLUGIN_CHANGE_COMMENT_LINE", DEFAULT_YAML_COMMENT).strip()
    if not comment_line.startswith("#"):
        comment_line = "# " + comment_line

    pr_backend = os.environ.get("PLUGIN_PR_BACKEND", "").strip().lower()
    github_api = os.environ.get("PLUGIN_GITHUB_API_URL", "https://api.github.com").strip()
    github_api_token = os.environ.get("PLUGIN_GITHUB_TOKEN", "").strip() or token

    harness_platform = os.environ.get("PLUGIN_HARNESS_PLATFORM_URL", "https://app.harness.io").strip()
    harness_api_key = os.environ.get("PLUGIN_HARNESS_API_KEY", "").strip()
    harness_repo_id = os.environ.get("PLUGIN_HARNESS_REPO_IDENTIFIER", "").strip()
    harness_account = os.environ.get("PLUGIN_HARNESS_ACCOUNT_IDENTIFIER", "").strip()
    harness_org = os.environ.get("PLUGIN_HARNESS_ORG_IDENTIFIER", "").strip()
    harness_project = os.environ.get("PLUGIN_HARNESS_PROJECT_IDENTIFIER", "").strip()

    bitbucket_api_url = os.environ.get(
        "PLUGIN_BITBUCKET_API_URL", "https://api.bitbucket.org/2.0"
    ).strip()
    bitbucket_workspace = os.environ.get("PLUGIN_BITBUCKET_WORKSPACE", "").strip()
    bitbucket_repo_slug = os.environ.get("PLUGIN_BITBUCKET_REPO_SLUG", "").strip()
    bitbucket_username = os.environ.get("PLUGIN_BITBUCKET_USERNAME", "").strip() or username
    bitbucket_app_password = (
        os.environ.get("PLUGIN_BITBUCKET_APP_PASSWORD", "").strip() or token
    )
    bitbucket_access_token = os.environ.get("PLUGIN_BITBUCKET_ACCESS_TOKEN", "").strip()

    if not repo_url:
        raise PluginError("PLUGIN_REPO_URL is required")
    if not ticket:
        raise PluginError("PLUGIN_CHANGE_TICKET is required (e.g. CHG-001)")

    cfg = {
        "repo_url": repo_url,
        "ticket": ticket,
        "harness_path": harness_path,
        "base_branch": base_branch,
        "branch_prefix": branch_prefix,
        "work_dir": Path(work_dir),
        "username": username,
        "token": token,
        "git_name": git_name,
        "git_email": git_email,
        "title_tmpl": title_tmpl,
        "comment_line": comment_line,
        "pr_backend": pr_backend,
        "github_api_url": github_api,
        "github_api_token": github_api_token,
        "harness_platform_url": harness_platform,
        "harness_api_key": harness_api_key,
        "harness_repo_identifier": harness_repo_id,
        "harness_account_identifier": harness_account,
        "harness_org_identifier": harness_org,
        "harness_project_identifier": harness_project,
        "bitbucket_api_url": bitbucket_api_url,
        "bitbucket_workspace": bitbucket_workspace,
        "bitbucket_repo_slug": bitbucket_repo_slug,
        "bitbucket_username": bitbucket_username,
        "bitbucket_app_password": bitbucket_app_password,
        "bitbucket_access_token": bitbucket_access_token,
    }
    cfg["resolved_pr_backend"] = resolve_pr_backend(cfg)
    return cfg


def _to_camel_case(snake_str: str) -> str:
    components = snake_str.split("_")
    return components[0] + "".join(word.capitalize() for word in components[1:])


def write_drone_output(values: Dict[str, str]) -> None:
    path = os.environ.get("DRONE_OUTPUT")
    if not path:
        path = "/tmp/DRONE_OUTPUT"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{_to_camel_case(k)}={v}\n")


def main() -> int:
    pushed_branches: List[str] = []
    pr_records: List[Dict[str, Any]] = []
    try:
        cfg = load_config()
        backend = str(cfg["resolved_pr_backend"])
        clone_url = authenticated_clone_url(
            str(cfg["repo_url"]), str(cfg["username"]), str(cfg["token"])
        )

        if backend == "harness":
            if not cfg["harness_api_key"]:
                raise PluginError(
                    "Harness PR: set PLUGIN_HARNESS_API_KEY (x-api-key for Harness API)"
                )
            if not cfg["harness_repo_identifier"]:
                raise PluginError(
                    "Harness PR: set PLUGIN_HARNESS_REPO_IDENTIFIER (repo_identifier path)"
                )
            if not cfg["harness_account_identifier"]:
                raise PluginError("Harness PR: set PLUGIN_HARNESS_ACCOUNT_IDENTIFIER")
        elif backend == "github":
            if not cfg["github_api_token"]:
                raise PluginError(
                    "GitHub PR: set PLUGIN_GITHUB_TOKEN or PLUGIN_GIT_TOKEN for API"
                )
        elif backend == "bitbucket":
            try:
                resolve_bitbucket_cloud_workspace_repo(cfg)
            except PluginError as e:
                raise PluginError(
                    "Bitbucket PR: set PLUGIN_BITBUCKET_WORKSPACE and "
                    "PLUGIN_BITBUCKET_REPO_SLUG, or use a bitbucket.org clone URL. "
                    f"({e})"
                ) from e
            if not cfg["bitbucket_access_token"] and not (
                cfg["bitbucket_username"] and cfg["bitbucket_app_password"]
            ):
                raise PluginError(
                    "Bitbucket PR: set PLUGIN_BITBUCKET_ACCESS_TOKEN (OAuth2), or "
                    "username + app password via PLUGIN_BITBUCKET_USERNAME and "
                    "PLUGIN_BITBUCKET_APP_PASSWORD, or PLUGIN_GIT_USERNAME and "
                    "PLUGIN_GIT_TOKEN (same app password as HTTPS git)"
                )

        cfg["work_dir"].mkdir(parents=True, exist_ok=True)
        repo_folder_name = str(cfg["repo_url"]).split("/")[-1].replace(".git", "")
        repo_path = cfg["work_dir"] / repo_folder_name
        if repo_path.exists():
            shutil.rmtree(repo_path)

        logger.info("Cloning repository")
        subprocess.run(
            ["git", "clone", clone_url, str(repo_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        run_git(["remote", "set-url", "origin", clone_url], repo_path)

        run_git(["config", "user.email", str(cfg["git_email"])], repo_path)
        run_git(["config", "user.name", str(cfg["git_name"])], repo_path)

        yaml_files = discover_yaml_files(repo_path, str(cfg["harness_path"]))
        if not yaml_files:
            raise PluginError(
                f"No .yaml/.yml files under {cfg['harness_path']} in repository"
            )

        gh_owner: Optional[str] = None
        gh_repo: Optional[str] = None
        if backend == "github":
            gh_owner, gh_repo = parse_github_repo(str(cfg["repo_url"]))

        bb_workspace: Optional[str] = None
        bb_repo_slug: Optional[str] = None
        if backend == "bitbucket":
            bb_workspace, bb_repo_slug = resolve_bitbucket_cloud_workspace_repo(cfg)

        for ypath in yaml_files:
            rel = ypath.relative_to(repo_path).as_posix()
            service = slug_for_branch(ypath.stem)
            ticket_part = slug_for_branch(str(cfg["ticket"]))
            branch = f"{cfg['branch_prefix']}/{ticket_part}-{service}"

            title = str(cfg["title_tmpl"]).format(
                ticket=str(cfg["ticket"]), service=ypath.stem, path=rel
            )
            description = (
                f"Automated PR for change **{cfg['ticket']}**.\n\n"
                f"- File: `{rel}`\n"
                f"- Branch: `{branch}`\n"
                f"- Base: `{cfg['base_branch']}`\n"
            )

            logger.info("Syncing %s and creating branch %s", cfg["base_branch"], branch)
            run_git(["fetch", "origin", str(cfg["base_branch"])], repo_path)
            run_git(
                ["checkout", "-B", str(cfg["base_branch"]), f"origin/{cfg['base_branch']}"],
                repo_path,
            )

            run_git(["checkout", "-b", branch], repo_path)

            raw = ypath.read_text(encoding="utf-8")
            ypath.write_text(
                append_comment_line(raw, str(cfg["comment_line"])),
                encoding="utf-8",
            )

            msg = f"{cfg['ticket']}: marker comment + PR for {rel}"
            run_git(["add", rel], repo_path)
            run_git(["commit", "-m", msg], repo_path)

            logger.info("Pushing branch %s", branch)
            subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            pushed_branches.append(branch)

            pr_url = ""
            pr_number: Optional[int] = None
            if backend == "none":
                logger.info("PLUGIN_PR_BACKEND=none: skipping PR creation")
            elif backend == "github" and gh_owner and gh_repo:
                logger.info("Creating GitHub pull request")
                pr = create_pull_request_github(
                    str(cfg["github_api_url"]),
                    str(cfg["github_api_token"]),
                    gh_owner,
                    gh_repo,
                    title,
                    branch,
                    str(cfg["base_branch"]),
                    description,
                )
                pr_url = str(pr.get("html_url", "") or "")
                num = pr.get("number")
                pr_number = int(num) if isinstance(num, int) else None
                pr_records.append({"backend": "github", "url": pr_url, "number": pr_number})
                logger.info("PR opened: %s", pr_url or pr)
            elif backend == "bitbucket" and bb_workspace and bb_repo_slug:
                logger.info("Creating Bitbucket Cloud pull request")
                pr = create_pull_request_bitbucket_cloud(
                    str(cfg["bitbucket_api_url"]),
                    bb_workspace,
                    bb_repo_slug,
                    title,
                    branch,
                    str(cfg["base_branch"]),
                    description,
                    str(cfg["bitbucket_username"]),
                    str(cfg["bitbucket_app_password"]),
                    str(cfg["bitbucket_access_token"]),
                )
                pr_url = bitbucket_cloud_pr_html_url(pr)
                bid = pr.get("id")
                pr_number = int(bid) if isinstance(bid, int) else None
                pr_records.append(
                    {"backend": "bitbucket", "url": pr_url, "number": pr_number}
                )
                logger.info("PR opened: %s", pr_url or pr)
            elif backend == "harness":
                logger.info("Creating Harness Code pull request")
                pr = create_pull_request_harness(
                    str(cfg["harness_platform_url"]),
                    str(cfg["harness_api_key"]),
                    str(cfg["harness_repo_identifier"]),
                    str(cfg["harness_account_identifier"]),
                    str(cfg["harness_org_identifier"]),
                    str(cfg["harness_project_identifier"]),
                    title,
                    branch,
                    str(cfg["base_branch"]),
                    description,
                )
                num = pr.get("number")
                pr_number = int(num) if isinstance(num, int) else None
                pr_url = str(pr.get("url", "") or "")
                pr_records.append(
                    {
                        "backend": "harness",
                        "number": pr_number,
                        "title": pr.get("title"),
                        "source_branch": pr.get("source_branch"),
                        "target_branch": pr.get("target_branch"),
                    }
                )
                logger.info("Harness PR created: #%s %s", pr_number, title)

        pr_urls_out = [
            r.get("url", "")
            for r in pr_records
            if isinstance(r.get("url"), str) and r.get("url")
        ]
        write_drone_output(
            {
                "execution_status": "success",
                "pushed_branches": json.dumps(pushed_branches),
                "branch_count": str(len(pushed_branches)),
                "pull_request_urls": json.dumps(pr_urls_out),
                "pull_request_details": json.dumps(pr_records),
                "repo_url": sanitize_repo_url_for_output(str(cfg["repo_url"])),
                "pr_backend": backend,
            }
        )
        logger.info("Done: %d branch(es), PR backend=%s", len(pushed_branches), backend)
        return 0

    except PluginError as e:
        logger.error("%s", e)
        write_drone_output({"execution_status": "failed", "error_message": str(e)})
        return 1
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        logger.error("Command failed: %s", err)
        write_drone_output({"execution_status": "failed", "error_message": err or str(e)})
        return 1
    except Exception as e:
        logger.error("Unexpected: %s", e)
        write_drone_output({"execution_status": "failed", "error_message": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
