#!/usr/bin/env python3
"""
Drone plugin: clone a repo, append a marker comment to each YAML under a path,
write release manifest(s), push one topic branch for the change ticket, then
open a single pull request (Harness Code or GitHub API).

PLUGIN_MODE:
  standard (default) — release-manifest-<ticket>.yaml at repo root
  blue-green         — release-<ticket>/offline-<color>-services.yml and
                       online-<color>-services.yml
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlencode, urlparse

import yaml


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
    """Always append one marker line at the bottom (even if the same marker is already present)."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + comment_line.strip() + "\n"


def strip_marker_lines(text: str, marker_line: str) -> str:
    """Remove marker lines before YAML parse/update only (not used before write)."""
    m = marker_line.strip()
    lines = text.splitlines()
    kept = [ln for ln in lines if ln.strip() != m]
    out = "\n".join(kept).rstrip("\n")
    return out + ("\n" if out else "")


def update_input_set_yaml_values(yaml_obj: Any, rel_path: str, cfg: Dict[str, Any]) -> bool:
    """
    Update Harness InputSet YAML values based on file path and filename.

    Conventions:
    - offline/* or *_{offlineColor} or *_dr_{offlineColor}: offline ticket + offlineColor
    - route-change/* or *_route_change or *_{onlineColor}: online ticket + fromColor/toColor
    - flat input_sets/ folders: classify by filename suffix
    """
    if not isinstance(yaml_obj, dict):
        return False

    input_set = yaml_obj.get("inputSet") if "inputSet" in yaml_obj else yaml_obj
    if not isinstance(input_set, dict):
        return False

    pipeline = input_set.get("pipeline")
    if not isinstance(pipeline, dict):
        return False

    variables = pipeline.get("variables")
    if not isinstance(variables, list):
        return False

    rel_norm = rel_path.replace("\\", "/").lower()
    stem_lower = Path(rel_path).stem.lower()
    offline_color = str(cfg["offline_color"]).lower()
    online_color = str(cfg["online_color"]).lower()

    is_offline = (
        "/offline/" in rel_norm
        or rel_norm.endswith("/offline")
        or stem_lower.endswith(f"_{offline_color}")
        or stem_lower.endswith(f"_dr_{offline_color}")
        or "_offline" in stem_lower
    )
    is_route = (
        "/route-change/" in rel_norm
        or "/route_change/" in rel_norm
        or "/route-change" in rel_norm
        or "_route_change" in stem_lower
        or stem_lower.endswith(f"_{online_color}")
        or stem_lower.endswith(f"_dr_{online_color}")
    )

    if is_offline and is_route:
        # Prefer explicit route-change naming when both patterns match.
        if "_route_change" in stem_lower or "route-change" in rel_norm or "route_change" in rel_norm:
            is_offline = False
        else:
            is_route = False

    if not is_offline and not is_route:
        return False

    change_ticket_value = cfg["ticket"] if is_offline else cfg.get("online_ticket") or cfg["ticket"]
    release_version = cfg.get("release_version") or ""

    updated = False
    for v in variables:
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "")
        if name == "changeTicket":
            v["value"] = str(change_ticket_value)
            updated = True
        elif name == "offlineColor" and is_offline:
            v["value"] = str(cfg["offline_color"])
            updated = True
        elif name == "fromColor" and is_route:
            v["value"] = str(cfg["online_color"])
            updated = True
        elif name == "toColor" and is_route:
            v["value"] = str(cfg["offline_color"])
            updated = True
        elif name == "releaseVersion" and release_version:
            v["value"] = str(release_version)
            updated = True

    return updated


def slug_for_branch(name: str) -> str:
    """Git branch segment: alphanumeric, dot, underscore, hyphen."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip())
    return s.strip("-") or "service"


def release_manifest_filename(ticket_part: str) -> str:
    """Manifest file at repo root: release-manifest-<ticket>.yaml"""
    return f"release-manifest-{ticket_part}.yaml"


def release_dir_name(ticket_part: str) -> str:
    return f"release-{ticket_part}"


def offline_services_filename(color: str) -> str:
    return f"offline-{color}-services.yml"


def online_services_filename(color: str) -> str:
    return f"online-{color}-services.yml"


def yaml_manifest_key(stem: str) -> str:
    """Quote YAML mapping key when the stem is not a plain unquoted token."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+", stem):
        return stem
    return json.dumps(stem)


def build_standard_release_manifest_yaml(change_ticket: str, inputset_stems: List[str]) -> str:
    """Standard mode: ChangeTicket and services (one key per input set stem)."""
    lines: List[str] = [
        f"ChangeTicket: {change_ticket}",
        "services:",
        "  # Input set file names (without extension); all enabled for this release.",
    ]
    for stem in sorted(inputset_stems, key=lambda s: s.lower()):
        k = yaml_manifest_key(stem)
        lines.append(f"  {k}: true")
    return "\n".join(lines) + "\n"


def build_blue_green_services_manifest_yaml(change_ticket: str, service_stems: List[str]) -> str:
    """Blue-green mode: changeTicket + services map (each service: true by default)."""
    lines: List[str] = [
        f"changeTicket: {change_ticket}",
        "services:",
        "  # Service stems; set to false in PR review to skip a service for this release.",
    ]
    for stem in sorted(service_stems, key=lambda s: s.lower()):
        k = yaml_manifest_key(stem)
        lines.append(f"  {k}: true")
    return "\n".join(lines) + "\n"


def normalize_service_stem(name: str) -> str:
    """Strip optional .yml/.yaml suffix from a manifest service key."""
    if name.lower().endswith((".yaml", ".yml")):
        return Path(name).stem
    return name


def _normalize_color(color: str) -> str:
    return color.strip().lower()


def _stem_matches_dr_color(stem: str, color: str) -> bool:
    return stem.lower().endswith(f"_dr_{_normalize_color(color)}")


def _stem_matches_color(stem: str, color: str) -> bool:
    suffix = f"_{_normalize_color(color)}"
    return stem.lower().endswith(suffix) and not _stem_matches_dr_color(stem, color)


def _is_route_change_path(rel_path: str, stem: str) -> bool:
    rel_norm = rel_path.replace("\\", "/").lower()
    return (
        "_route_change" in stem.lower()
        or "/route-change/" in rel_norm
        or "/route_change/" in rel_norm
    )


def _is_offline_path(rel_path: str) -> bool:
    rel_norm = rel_path.replace("\\", "/").lower()
    return "/offline/" in rel_norm or rel_norm.endswith("/offline")


def _service_base_allowed(service_base: str, enabled_bases: List[str]) -> bool:
    if not enabled_bases:
        return True
    return service_base.lower() in {b.lower() for b in enabled_bases}


def _infer_service_base_from_offline_stem(stem: str) -> Optional[str]:
    for suffix in ("_offline_deploy", "_offline", "_deploy"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)].rstrip("_")
    return None


def _infer_service_base_from_route_stem(stem: str) -> Optional[str]:
    if stem.lower().endswith("_route_change"):
        return stem[: -len("_route_change")].rstrip("_")
    return None


def discover_blue_green_service_entries(
    yaml_files: List[Path],
    repo_path: Path,
    offline_color: str,
    online_color: str,
    enabled_service_bases: List[str],
) -> Tuple[List[str], List[str]]:
    """
    Build offline and online service stem lists from scanned YAML files.

    Matches:
    - <service>_<color> and <service>_dr_<color> by filename stem
    - offline/* input sets -> <service>_<offlineColor>
    - route-change/* input sets -> <service>_route_change (online manifest)
    """
    offline_entries: Set[str] = set()
    online_entries: Set[str] = set()

    for ypath in yaml_files:
        rel = ypath.relative_to(repo_path).as_posix()
        stem = ypath.stem

        if _stem_matches_dr_color(stem, offline_color) or _stem_matches_color(stem, offline_color):
            base = stem.rsplit("_dr_", 1)[0] if _stem_matches_dr_color(stem, offline_color) else stem.rsplit("_", 1)[0]
            if _service_base_allowed(base, enabled_service_bases):
                offline_entries.add(stem)
            continue

        if _stem_matches_dr_color(stem, online_color) or _stem_matches_color(stem, online_color):
            base = stem.rsplit("_dr_", 1)[0] if _stem_matches_dr_color(stem, online_color) else stem.rsplit("_", 1)[0]
            if _service_base_allowed(base, enabled_service_bases):
                online_entries.add(stem)
            continue

        if _is_route_change_path(rel, stem):
            base = _infer_service_base_from_route_stem(stem) or stem
            if _service_base_allowed(base, enabled_service_bases):
                online_entries.add(f"{base}_route_change")
            continue

        if _is_offline_path(rel):
            base = _infer_service_base_from_offline_stem(stem)
            if base and _service_base_allowed(base, enabled_service_bases):
                offline_entries.add(f"{base}_{offline_color}")

    if not offline_entries:
        raise PluginError(
            f"No offline services found for color '{offline_color}'. "
            "Expected filenames ending with _<color> or _dr_<color>, or input sets under offline/."
        )
    if not online_entries:
        raise PluginError(
            f"No online services found for color '{online_color}' or route-change input sets. "
            "Expected filenames ending with _<color>, _dr_<color>, or route-change/ input sets."
        )

    return sorted(offline_entries), sorted(online_entries)


def discover_yaml_files(root: Path, relative_dir: str) -> List[Path]:
    base = root / relative_dir.strip("/ ")
    if not base.is_dir():
        raise PluginError(
            f"Harness path is not a directory: {relative_dir} "
            f"(resolved: {base}). Check PLUGIN_HARNESS_PATH."
        )
    files: List[Path] = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".yaml", ".yml"):
            files.append(p)
    logger.info("Discovered %d YAML file(s) under %s", len(files), relative_dir)
    for p in files:
        logger.info("  - %s", p.relative_to(root).as_posix())
    return files


def git_add_path(repo_path: Path, rel: str, force: bool = False) -> None:
    """Stage a path; use -f when .gitignore would otherwise exclude input sets."""
    args = ["add", "-f", rel] if force else ["add", rel]
    proc = run_git(args, repo_path, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise PluginError(f"git add failed for {rel}: {err}")


def list_ignored_paths(repo_path: Path, rel_paths: List[str]) -> List[str]:
    ignored: List[str] = []
    for rel in rel_paths:
        proc = run_git(["check-ignore", "-q", rel], repo_path, check=False)
        if proc.returncode == 0:
            ignored.append(rel)
    return ignored


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
    harness_ready = (
        cfg.get("harness_api_key")
        and cfg.get("harness_repo_identifier")
        and cfg.get("harness_account_identifier")
    )
    if harness_ready:
        return "harness"
    repo = str(cfg.get("repo_url") or "")
    if "github.com" in repo:
        return "github"
    raise PluginError(
        "Cannot determine PR backend: set PLUGIN_PR_BACKEND=harness or github, or none. "
        "Harness: PLUGIN_HARNESS_API_KEY, PLUGIN_HARNESS_REPO_IDENTIFIER, "
        "PLUGIN_HARNESS_ACCOUNT_IDENTIFIER. GitHub: host github.com and "
        "PLUGIN_GITHUB_TOKEN or PLUGIN_GIT_TOKEN."
    )


def load_config() -> Dict[str, Any]:
    repo_url = os.environ.get("PLUGIN_REPO_URL", "").strip()
    ticket = os.environ.get("PLUGIN_CHANGE_TICKET", "").strip()
    mode = os.environ.get("PLUGIN_MODE", "standard").strip().lower() or "standard"
    online_ticket = os.environ.get("PLUGIN_ONLINE_CHANGE_TICKET", "").strip()
    offline_color = os.environ.get("PLUGIN_OFFLINE_COLOR", "blue").strip() or "blue"
    online_color = os.environ.get("PLUGIN_ONLINE_COLOR", "green").strip() or "green"
    release_version = os.environ.get("PLUGIN_RELEASE_VERSION", "").strip()
    service_bases_csv = os.environ.get("PLUGIN_SERVICE_BASES", "").strip()
    enabled_service_bases = (
        [s.strip() for s in service_bases_csv.split(",") if s.strip()] if service_bases_csv else []
    )
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
        "InputSets for the release of change ticket {ticket}",
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

    if not repo_url:
        raise PluginError("PLUGIN_REPO_URL is required")
    if not ticket:
        raise PluginError("PLUGIN_CHANGE_TICKET is required (e.g. CHG-001)")
    if mode not in ("standard", "blue-green", "blue_green"):
        raise PluginError("PLUGIN_MODE must be 'standard' or 'blue-green'")
    if mode in ("blue-green", "blue_green"):
        mode = "blue-green"

    cfg = {
        "repo_url": repo_url,
        "ticket": ticket,
        "mode": mode,
        "online_ticket": online_ticket or ticket,
        "offline_color": offline_color,
        "online_color": online_color,
        "release_version": release_version,
        "enabled_service_bases": enabled_service_bases,
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
        mode = str(cfg["mode"])
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

        cfg["work_dir"].mkdir(parents=True, exist_ok=True)
        repo_folder_name = str(cfg["repo_url"]).split("/")[-1].replace(".git", "")
        repo_path = cfg["work_dir"] / repo_folder_name
        if repo_path.exists():
            shutil.rmtree(repo_path)

        logger.info("Cloning repository (mode=%s)", mode)
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

        ticket_part = slug_for_branch(str(cfg["ticket"]))
        branch = f"{cfg['branch_prefix']}/{ticket_part}"

        manifest_paths: List[str] = []
        manifest_yaml: str = ""
        manifest_services: List[str] = []

        if mode == "blue-green":
            release_dir = release_dir_name(ticket_part)
            offline_manifest = offline_services_filename(str(cfg["offline_color"]))
            online_manifest = online_services_filename(str(cfg["online_color"]))
            offline_rel = f"{release_dir}/{offline_manifest}"
            online_rel = f"{release_dir}/{online_manifest}"
            manifest_paths = [offline_rel, online_rel]
        else:
            manifest_name = release_manifest_filename(ticket_part)
            manifest_paths = [manifest_name]

        title = str(cfg["title_tmpl"]).format(
            ticket=str(cfg["ticket"]),
            service="",
            path="",
        )

        file_list = "\n".join(
            f"- `{p.relative_to(repo_path).as_posix()}`" for p in yaml_files
        )
        manifest_list = "\n".join(f"- `{m}`" for m in manifest_paths)
        description = (
            f"Automated PR for change **{cfg['ticket']}** (mode: `{mode}`).\n\n"
            f"- Manifest(s):\n{manifest_list}\n"
            f"- Branch: `{branch}`\n"
            f"- Base: `{cfg['base_branch']}`\n\n"
            f"**Updated YAML files:**\n\n{file_list}\n"
        )

        logger.info("Syncing %s and creating branch %s", cfg["base_branch"], branch)
        run_git(["fetch", "origin", str(cfg["base_branch"])], repo_path)
        run_git(
            ["checkout", "-B", str(cfg["base_branch"]), f"origin/{cfg['base_branch']}"],
            repo_path,
        )
        run_git(["checkout", "-b", branch], repo_path)

        stem_counts = Counter(p.stem for p in yaml_files)

        def manifest_key_for(yp: Path) -> str:
            if stem_counts[yp.stem] > 1:
                rel_no_ext = yp.relative_to(repo_path).as_posix().rsplit(".", 1)[0]
                return rel_no_ext.replace("/", "-")
            return yp.stem

        stems: List[str] = []
        rel_paths: List[str] = []
        for ypath in yaml_files:
            rel = ypath.relative_to(repo_path).as_posix()
            rel_paths.append(rel)
            stems.append(manifest_key_for(ypath))
            raw = ypath.read_text(encoding="utf-8")
            marker = str(cfg["comment_line"])
            parse_source = strip_marker_lines(raw, marker)
            yaml_updated = False
            updated_text = parse_source
            try:
                yaml_obj = yaml.safe_load(parse_source)
                if yaml_obj and update_input_set_yaml_values(yaml_obj, rel, cfg):
                    updated_text = yaml.safe_dump(
                        yaml_obj,
                        sort_keys=False,
                        default_flow_style=False,
                    )
                    yaml_updated = True
            except Exception:
                logger.warning(
                    "Failed YAML parse/update for %s (keeping original content)",
                    rel,
                    exc_info=True,
                )
            body = updated_text if yaml_updated else raw
            ypath.write_text(append_comment_line(body, marker), encoding="utf-8")

        if mode == "blue-green":
            offline_entries, online_entries = discover_blue_green_service_entries(
                yaml_files,
                repo_path,
                str(cfg["offline_color"]),
                str(cfg["online_color"]),
                list(cfg.get("enabled_service_bases") or []),
            )
            release_dir_path = repo_path / release_dir_name(ticket_part)
            release_dir_path.mkdir(parents=True, exist_ok=True)

            offline_yaml = build_blue_green_services_manifest_yaml(
                str(cfg["ticket"]), offline_entries
            )
            online_yaml = build_blue_green_services_manifest_yaml(
                str(cfg.get("online_ticket") or cfg["ticket"]), online_entries
            )
            (release_dir_path / offline_services_filename(str(cfg["offline_color"]))).write_text(
                offline_yaml, encoding="utf-8"
            )
            (release_dir_path / online_services_filename(str(cfg["online_color"]))).write_text(
                online_yaml, encoding="utf-8"
            )
            manifest_services = offline_entries + online_entries
            manifest_yaml = offline_yaml + "\n---\n" + online_yaml
        else:
            manifest_name = manifest_paths[0]
            manifest_yaml = build_standard_release_manifest_yaml(str(cfg["ticket"]), stems)
            manifest_services = stems
            (repo_path / manifest_name).write_text(manifest_yaml, encoding="utf-8")

        harness_rel = str(cfg["harness_path"]).strip().strip("/")
        ignored = list_ignored_paths(repo_path, rel_paths)
        if ignored:
            logger.warning(
                "%d input set file(s) are listed in .gitignore and need force-add: %s",
                len(ignored),
                ", ".join(ignored),
            )

        git_add_path(repo_path, harness_rel, force=True)
        for rel in rel_paths:
            git_add_path(repo_path, rel, force=True)
        for manifest_rel in manifest_paths:
            git_add_path(repo_path, manifest_rel, force=False)

        staged_proc = run_git(["diff", "--cached", "--name-only"], repo_path, check=False)
        staged_files = [
            ln.strip() for ln in (staged_proc.stdout or "").splitlines() if ln.strip()
        ]
        logger.info("Staged %d file(s) for commit", len(staged_files))

        msg = f"{cfg['ticket']}: marker comments on input sets + {', '.join(manifest_paths)}"
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
                    "url": pr_url,
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
                "plugin_mode": mode,
                "change_ticket": str(cfg["ticket"]),
                "offline_color": str(cfg["offline_color"]),
                "online_color": str(cfg["online_color"]),
                "manifest_services": json.dumps(manifest_services),
                "release_manifest_yaml_json": json.dumps(manifest_yaml),
                "harness_path": str(cfg["harness_path"]),
                "input_set_file_count": str(len(rel_paths)),
                "staged_files": json.dumps(staged_files),
            }
        )
        logger.info("Done: %d branch(es), PR backend=%s, mode=%s", len(pushed_branches), backend, mode)
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
