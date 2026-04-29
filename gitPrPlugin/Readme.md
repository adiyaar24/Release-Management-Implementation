# Drone plugin: multi-branch YAML + pull requests

The plugin **clones** your repo, **edits** each `.yaml` / `.yml` under a path, **pushes** one topic branch per file, then **creates a pull request** per branch.

- **Git** (clone, commit, push) works with any remote, same idea as **`drone_git_plugin.py`** (Commit To Git).
- **PR creation** uses a **Host API** (not git): **Harness Code** (default when Harness settings are set) or **GitHub**. Set `PLUGIN_PR_BACKEND=none` to only push branches.

## What each PR changes

At the **end of each touched YAML file** the plugin appends a **comment line** (and a trailing newline). Default text:

```text
# Remove this comment post your chnges are done , this was created as part of auto creation of PR for easier view
```

Override with **`PLUGIN_CHANGE_COMMENT_LINE`** (if the value does not start with `#`, a `#` prefix is added for YAML).

## Branches and base

- **Base branch:** `main` by default (`PLUGIN_BASE_BRANCH`).
- **Topic branch:** `release/<change-ticket>-<service>` (e.g. `service1.yaml` + `CHG-001` → `release/CHG-001-service1`).

## PR backend selection

| `PLUGIN_PR_BACKEND` | Behavior |
|----------------------|----------|
| *(unset)* | **Auto:** if Harness API settings are present → **harness**; if `github.com` in `PLUGIN_REPO_URL` → **github**; otherwise the run **fails** with a hint to set backend or vars. |
| `harness` | Harness Code REST API (`x-api-key`). |
| `github` | GitHub REST API (`Bearer` token). |
| `none` | Push branches only; **no** PR API calls. |

## Environment variables

### Required (always)

| Variable | Description |
|----------|-------------|
| `PLUGIN_REPO_URL` | Clone URL (`https://...` or `git@...`). |
| `PLUGIN_CHANGE_TICKET` | Change id for branch names, e.g. `CHG-001`. |

### Git (same pattern as Commit To Git)

| Variable | Default | Description |
|----------|---------|-------------|
| `PLUGIN_GIT_USERNAME` | *(empty)* | HTTPS username; used with `PLUGIN_GIT_TOKEN`. |
| `PLUGIN_GIT_TOKEN` | *(empty)* | Password / PAT for HTTPS clone and push. |
| `PLUGIN_HARNESS_PATH` | `.harness` | Directory under repo root to scan (recursive). |
| `PLUGIN_BASE_BRANCH` | `main` | PR target / branch fork point. |
| `PLUGIN_BRANCH_PREFIX` | `release` | Branch prefix. |
| `PLUGIN_WORK_DIR` | `/harness` | Parent directory for the clone. |
| `PLUGIN_GIT_AUTHOR_NAME` | `Drone PR Plugin` | Commit author name. |
| `PLUGIN_GIT_AUTHOR_EMAIL` | `drone-pr-plugin@local` | Commit author email. |
| `PLUGIN_PR_TITLE_TEMPLATE` | `[{ticket}] {service} harness update` | PR title; `{ticket}`, `{service}`, `{path}`. |
| `PLUGIN_CHANGE_COMMENT_LINE` | *(see default above)* | Marker line appended at bottom of each YAML. |

### Harness Code PR API (when backend is harness)

| Variable | Description |
|----------|-------------|
| `PLUGIN_HARNESS_API_KEY` | Harness **API key** (`x-api-key` header). Not necessarily the same as the git password. |
| `PLUGIN_HARNESS_REPO_IDENTIFIER` | Path segment `repo_identifier` for `POST /code/api/v1/repos/{repo_identifier}/pullreq`. |
| `PLUGIN_HARNESS_ACCOUNT_IDENTIFIER` | Query `accountIdentifier` (**required**). |
| `PLUGIN_HARNESS_ORG_IDENTIFIER` | Query `orgIdentifier` (optional). |
| `PLUGIN_HARNESS_PROJECT_IDENTIFIER` | Query `projectIdentifier` (optional). |
| `PLUGIN_HARNESS_PLATFORM_URL` | Default `https://app.harness.io`. Use your Harness / vanity URL if different. |

### GitHub PR API (when backend is github)

| Variable | Default | Description |
|----------|---------|-------------|
| `PLUGIN_GITHUB_TOKEN` | falls back to `PLUGIN_GIT_TOKEN` | PAT with `repo` + PR scope. |
| `PLUGIN_GITHUB_API_URL` | `https://api.github.com` | GitHub Enterprise API root if needed. |

### Drone outputs (`DRONE_OUTPUT`)

- `executionStatus`, `repoUrl`, `prBackend`
- `pushedBranches` — JSON array of branch names
- `branchCount`
- `pullRequestUrls` — JSON array (GitHub **html_url** when available; Harness often has no URL in API response)
- `pullRequestDetails` — JSON array of per-PR metadata from the API

If `DRONE_OUTPUT` is unset, outputs append to `/tmp/DRONE_OUTPUT`.

## Build

```bash
docker build -t your-registry/drone-pr-plugin:1.0.0 .
```

## Test locally

The plugin hits your **real Git remote** and **real PR APIs**. Use a **throwaway repo** and **test credentials**.

### SSH / HTTPS

For HTTPS private repos, set `PLUGIN_GIT_USERNAME` and `PLUGIN_GIT_TOKEN`. For `git@...`, ensure SSH keys/agent work in that shell or Docker container.

### Example: Harness Code

```bash
cd /path/to/gitPrCreation

export PLUGIN_REPO_URL="https://git.harness.io/your-org/your-repo.git"
export PLUGIN_CHANGE_TICKET="CHG-TEST-001"
export PLUGIN_HARNESS_PATH=".harness"
export PLUGIN_BASE_BRANCH="main"
export PLUGIN_GIT_USERNAME="..."
export PLUGIN_GIT_TOKEN="..."

export PLUGIN_PR_BACKEND="harness"
export PLUGIN_HARNESS_PLATFORM_URL="https://app.harness.io"
export PLUGIN_HARNESS_API_KEY="pat_or_api_key_from_harness"
export PLUGIN_HARNESS_REPO_IDENTIFIER="your/repo/identifier"
export PLUGIN_HARNESS_ACCOUNT_IDENTIFIER="your_account_id"
export PLUGIN_HARNESS_ORG_IDENTIFIER=""
export PLUGIN_HARNESS_PROJECT_IDENTIFIER=""

export PLUGIN_WORK_DIR="/tmp/pr-plugin-work"
export DRONE_OUTPUT="/tmp/drone-pr-plugin-test.out"

python3 drone_pr_plugin.py
cat /tmp/drone-pr-plugin-test.out
```

### Example: GitHub

```bash
export PLUGIN_REPO_URL="https://github.com/org/repo.git"
export PLUGIN_CHANGE_TICKET="CHG-TEST-001"
export PLUGIN_GIT_USERNAME="x-access-token"
export PLUGIN_GIT_TOKEN="ghp_..."
export PLUGIN_GITHUB_TOKEN="$PLUGIN_GIT_TOKEN"
export PLUGIN_PR_BACKEND="github"
python3 drone_pr_plugin.py
```

### Docker

Pass the same variables with `docker run -e ...`; mount a file for `DRONE_OUTPUT` if you want to inspect results.

### What to verify

- Branches exist: `release/<ticket>-<service>`.
- Each YAML ends with the **marker comment** line.
- PRs exist in Harness / GitHub for each branch into `PLUGIN_BASE_BRANCH`.

## Drone step example (Harness)

```yaml
steps:
  - name: harness-prs-per-service
    image: your-registry/drone-pr-plugin:1.0.0
    environment:
      PLUGIN_REPO_URL: https://git.harness.io/org/proj/repo.git
      PLUGIN_CHANGE_TICKET: CHG-001
      PLUGIN_HARNESS_PATH: .harness
      PLUGIN_BASE_BRANCH: main
      PLUGIN_GIT_USERNAME: git
      PLUGIN_GIT_TOKEN:
        from_secret: harness_git_token
      PLUGIN_PR_BACKEND: harness
      PLUGIN_HARNESS_PLATFORM_URL: https://app.harness.io
      PLUGIN_HARNESS_API_KEY:
        from_secret: harness_api_key
      PLUGIN_HARNESS_REPO_IDENTIFIER: org/project/repo-id
      PLUGIN_HARNESS_ACCOUNT_IDENTIFIER: accountId
      PLUGIN_HARNESS_PROJECT_IDENTIFIER: projectId
```

## Comparison to Commit To Git (`drone_git_plugin.py`)

| | Commit To Git | This plugin |
|--|----------------|-------------|
| Git | Clone + push | Clone + push **topic branches** |
| Remote change | JSON file → **main** | Marker comment on YAML → **PR** into base |
| Extra | None | PR via **Harness or GitHub API** |

## Notes

- **Harness `repo_identifier`:** take the value from Harness Code / API docs for your repository (not always the same string as the git URL).
- Re-runs may **force-push** or update existing branch names depending on remote state; delete old branches if you need a clean retry.
- Store API keys and git tokens in **Drone secrets**.
