# Drone plugin: single-branch input set PR + release manifest

The plugin **clones** your repo, **edits** each `.yaml` / `.yml` under a path, writes **release manifest(s)**, pushes **one topic branch** per change ticket, then opens **one pull request** (Harness Code or GitHub API).

- **Git** (clone, commit, push) works with any remote, same idea as **`drone_git_plugin.py`** (Commit To Git).
- **PR creation** uses **Harness Code** or **GitHub** REST API. Set `PLUGIN_PR_BACKEND=none` to push the branch only.

## Plugin modes (`PLUGIN_MODE`)

| Mode | Default | Manifest output |
|------|---------|-----------------|
| `standard` | **yes** | `release-manifest-<ticket>.yaml` at repo root |
| `blue-green` | no | `release-<ticket>/offline-<offlineColor>-services.yml` and `online-<onlineColor>-services.yml` |

### Standard mode

1. Appends a **marker comment** at the end of every YAML file under `PLUGIN_HARNESS_PATH` (default `.harness`).
2. Creates **`release-manifest-<ticket>.yaml`** at the repository root:

```yaml
ChangeTicket: CHG-001
services:
  env-inputSetService1: true
  env-inputSetService2: true
```

3. Commits everything on branch **`release/<ticket>`** and opens **one PR** into the base branch.

### Blue-green mode

Same marker comment + input-set value filling, plus a **`release-<ticket>/`** directory:

```
release-CHG0000001/
├── offline-blue-services.yml
└── online-green-services.yml
```

**`offline-blue-services.yml`**

```yaml
changeTicket: CHG0000001
services:
  nginx_blue: true
  alpine_blue: true
  service1_dr_blue: true
```

**`online-green-services.yml`** (preferred — flat map, no list dashes)

```yaml
changeTicket: CHG0000002
services:
  service1_goldtier_green: true
  service1_goldtier_dr_green: true
  nginx_route_change: true
  alpine_route_change: true
```

List-of-maps format (`- service: true`) is also accepted by the evaluation script, but the plugin generates the flat map above.

Each service defaults to `true`. Set a service to `false` in the PR review to skip it for that release (same toggle model as standard mode).

Service discovery scans `PLUGIN_HARNESS_PATH` recursively:

| Pattern | Offline manifest | Online manifest |
|---------|------------------|-----------------|
| `<service>_<offlineColor>.yml` | yes | |
| `<service>_dr_<offlineColor>.yml` | yes | |
| `<service>_<onlineColor>.yml` | | yes |
| `<service>_dr_<onlineColor>.yml` | | yes |
| `offline/*` input sets (e.g. `nginx_offline_deploy.yaml`) | maps to `nginx_<offlineColor>.yml` | |
| `route-change/*` input sets | | maps to `<service>_route_change.yml` |

Use **`PLUGIN_SERVICE_BASES`** (comma-separated, e.g. `nginx,alpine`) to limit which microservices are included.

Default marker comment (override with `PLUGIN_CHANGE_COMMENT_LINE`). The plugin **always appends** one marker line at the bottom of each input set YAML. If a marker is already present, another is added **below** it so the PR shows a diff. Remove extra marker lines manually after review/merge.

```text
# Remove this comment post your chnges are done , this was created as part of auto creation of PR for easier view
```

## Branches and base

- **Base branch:** `main` by default (`PLUGIN_BASE_BRANCH`).
- **Topic branch:** `release/<change-ticket>` (e.g. `CHG-001` → `release/CHG-001`).

## PR backend selection

| `PLUGIN_PR_BACKEND` | Behavior |
|----------------------|----------|
| *(unset)* | **Auto:** Harness API settings present → **harness**; `github.com` in URL → **github**; else fail with hint |
| `harness` | Harness Code REST API (`x-api-key`). |
| `github` | GitHub REST API (`Bearer` token). |
| `none` | Push branch only; **no** PR API call. |

## Environment variables

### Required

| Variable | Description |
|----------|-------------|
| `PLUGIN_REPO_URL` | Clone URL (`https://...` or `git@...`). |
| `PLUGIN_CHANGE_TICKET` | Change id for branch + manifest, e.g. `CHG-001`. |

### Mode and blue-green

| Variable | Default | Description |
|----------|---------|-------------|
| `PLUGIN_MODE` | `standard` | `standard` or `blue-green`. |
| `PLUGIN_OFFLINE_COLOR` | `blue` | Offline color for blue-green manifests and input-set filling. |
| `PLUGIN_ONLINE_COLOR` | `green` | Online color for blue-green manifests and input-set filling. |
| `PLUGIN_ONLINE_CHANGE_TICKET` | same as `PLUGIN_CHANGE_TICKET` | `changeTicket` in online manifest and route-change input sets. |
| `PLUGIN_RELEASE_VERSION` | *(empty)* | Sets `releaseVersion` in offline input sets when present. |
| `PLUGIN_SERVICE_BASES` | *(empty)* | Comma-separated microservice bases to include (e.g. `nginx,alpine`). |

### Git

| Variable | Default | Description |
|----------|---------|-------------|
| `PLUGIN_GIT_USERNAME` | *(empty)* | HTTPS username; used with `PLUGIN_GIT_TOKEN`. |
| `PLUGIN_GIT_TOKEN` | *(empty)* | Password / PAT for HTTPS clone and push. |
| `PLUGIN_HARNESS_PATH` | `.harness` | Directory under repo root to scan (recursive). Use full path to your input sets folder, e.g. `.harness/orgs/default/projects/BlueGreen/pipelines/Release_PipelineBG/input_sets`. |
| `PLUGIN_BASE_BRANCH` | `main` | PR target / branch fork point. |
| `PLUGIN_BRANCH_PREFIX` | `release` | Branch prefix. |
| `PLUGIN_WORK_DIR` | `/harness` | Parent directory for the clone. |
| `PLUGIN_GIT_AUTHOR_NAME` | `Drone PR Plugin` | Commit author name. |
| `PLUGIN_GIT_AUTHOR_EMAIL` | `drone-pr-plugin@local` | Commit author email. |
| `PLUGIN_PR_TITLE_TEMPLATE` | `InputSets for the release of change ticket {ticket}` | PR title; `{ticket}`, `{service}`, `{path}`. |
| `PLUGIN_CHANGE_COMMENT_LINE` | *(see default above)* | Marker line appended at bottom of each YAML. |

### Harness Code PR API

| Variable | Description |
|----------|-------------|
| `PLUGIN_HARNESS_API_KEY` | Harness API key (`x-api-key`). |
| `PLUGIN_HARNESS_REPO_IDENTIFIER` | Repo identifier for `POST .../pullreq`. |
| `PLUGIN_HARNESS_ACCOUNT_IDENTIFIER` | Query `accountIdentifier` (**required**). |
| `PLUGIN_HARNESS_ORG_IDENTIFIER` | Query `orgIdentifier` (optional). |
| `PLUGIN_HARNESS_PROJECT_IDENTIFIER` | Query `projectIdentifier` (optional). |
| `PLUGIN_HARNESS_PLATFORM_URL` | Default `https://app.harness.io`. |

### GitHub PR API

| Variable | Default | Description |
|----------|---------|-------------|
| `PLUGIN_GITHUB_TOKEN` | falls back to `PLUGIN_GIT_TOKEN` | PAT with `repo` + PR scope. |
| `PLUGIN_GITHUB_API_URL` | `https://api.github.com` | GitHub Enterprise API root if needed. |

### Drone outputs (`DRONE_OUTPUT`)

- `executionStatus`, `repoUrl`, `prBackend`, `pluginMode`
- `pushedBranches` — JSON array (one branch)
- `branchCount`
- `pullRequestUrls` — JSON array
- `pullRequestDetails` — JSON array of PR metadata
- `changeTicket`, `offlineColor`, `onlineColor`
- `manifestServices` — JSON array of manifest service entries
- `releaseManifestYamlJson` — JSON-escaped manifest content

## Build

```bash
docker build -t your-registry/drone-pr-plugin:1.0.0 .
```

## Drone / Harness CI step example (blue-green)

```yaml
- step:
    type: Plugin
    name: Create Input Set PR
    identifier: Create_InputSet_PR
    spec:
      connectorRef: account.dockerhub
      image: your-registry/drone-pr-plugin:1.0.0
      settings:
        REPO_URL: https://git.harness.io/.../elevance-blue-green-solutioning.git
        CHANGE_TICKET: <+pipeline.variables.offlineChangeTicket>
        MODE: blue-green
        OFFLINE_COLOR: <+pipeline.variables.offlineColor>
        ONLINE_COLOR: <+pipeline.variables.onlineColor>
        ONLINE_CHANGE_TICKET: <+pipeline.variables.onlineChangeTicket>
        HARNESS_PATH: .harness
        BASE_BRANCH: main
        GIT_USERNAME: <account_or_user>
        GIT_TOKEN: <+secrets.getValue("harness_git_token")>
        PR_BACKEND: harness
        HARNESS_API_KEY: <+secrets.getValue("harness_api_key")>
        HARNESS_REPO_IDENTIFIER: elevance-blue-green-solutioning
        HARNESS_ACCOUNT_IDENTIFIER: Npsd6WrETY-Baq6iHeOHGw
        HARNESS_ORG_IDENTIFIER: default
        HARNESS_PROJECT_IDENTIFIER: ElevanceHealth
```

## Manifest evaluation scripts

After the PR is merged, a **ShellScript** step reads the manifest and exports the same outputs for both modes.

| Script | Mode | Manifest path |
|--------|------|----------------|
| `scripts/evaluate-standard-release-manifest.sh` | `standard` | `release-manifest-<ticket>.yaml` |
| `scripts/evaluate-blue-green-release-manifest.sh` | `blue-green` | `release-<ticket>/offline-<color>-services.yml` or `online-<color>-services.yml` |

Both export `ENABLED_SERVICES_JSON` and `SKIPPED_SERVICES_JSON` for downstream Harness steps.

### Standard (your existing logic)

```bash
CHANGE_TICKET=<+pipeline.variables.changeTicket>
source scripts/evaluate-standard-release-manifest.sh
```

### Blue-green — offline deploy pipeline

```bash
CHANGE_TICKET=<+pipeline.variables.offlineChangeTicket>
ONLINE_CHANGE_TICKET=<+pipeline.variables.onlineChangeTicket>
OFFLINE_COLOR=<+pipeline.variables.offlineColor>
ONLINE_COLOR=<+pipeline.variables.onlineColor>
RELEASE_PHASE=offline
HARNESS_PATH=.harness
source scripts/evaluate-blue-green-release-manifest.sh
```

### Blue-green — route-change pipeline

```bash
CHANGE_TICKET=<+pipeline.variables.offlineChangeTicket>
ONLINE_CHANGE_TICKET=<+pipeline.variables.onlineChangeTicket>
OFFLINE_COLOR=<+pipeline.variables.offlineColor>
ONLINE_COLOR=<+pipeline.variables.onlineColor>
RELEASE_PHASE=online
HARNESS_PATH=.harness
source scripts/evaluate-blue-green-release-manifest.sh
```

Blue-green manifests use the same **`services.<name>: true|false`** toggle model as standard mode. Skipped services are those set to `false` in the manifest.

**Important:** Harness ShellScript steps must use **`shell: Bash`** (not `Sh`). The evaluation scripts re-exec with bash if needed.

## What to verify

- Branch exists: `release/<ticket>`.
- **PR includes input set YAML files** under `PLUGIN_HARNESS_PATH` (marker comment appended), not only `release-<ticket>/` manifests.
- If input sets are missing from the PR, check `.gitignore` — the plugin uses `git add -f` for the harness path (v1.1+).
- Plugin logs list every discovered YAML and every staged file before commit.
