# Release Management Implementation

Reference assets for a **Harness-based release workflow**: gated approvals, ServiceNow change validation, environment deployments, and automation that opens pull requests for Harness YAML.

## What’s in this repo

| Path | Purpose |
|------|--------|
| `releaseProcess.yaml` | **Release process definition** — multi-persona flow (Release Manager, Developer, Performance, QA): change validation, staging, parallel/final approvals, Staging/Prod deploy, and post-deploy tracking. |
| `Pipelines/` | **Harness pipeline YAML** — e.g. validate a ServiceNow change and drive PR creation; tiered approvals + UAT deploy; build/push the Drone PR plugin image. |
| `gitPrPlugin/` | **Drone plugin** (Python + Docker) — clone a repo, touch YAML under a path, push one branch per file, open PRs (Harness Code, GitHub, or Bitbucket Cloud). See `gitPrPlugin/Readme.md` for env vars, backends, and examples. |
| `inputSetRepoExample/` | **Example layout** for a pipeline repo with environment folders and per-service input sets (`pipeline.yaml`, `env/`, `env2/`). Replace placeholders with your real definitions. |

## Using this repo

- Import or adapt the pipeline definitions in Harness and wire **variables, connectors, and project/org identifiers** to your account.
- Align `releaseProcess.yaml` with your Release Orchestration or governance model if you use that feature.
- Build the PR plugin from `gitPrPlugin/` when you need the container referenced by CI steps.

This repository is a **starting point**; org-specific IDs, secrets, and connectors are left as placeholders or inputs.
