# Branch protection rulesets

This repository uses two GitHub branch protection rulesets.

## Per-repo rules

1. **Default branch (main)**  
   - Target: branch name `main`.  
   - Require a pull request, stale review dismissal, and resolved review threads (self-approval allowed; no mandatory external approval).  
   - Require status checks: **Analyze (python)**, **Unit tests (3.11)**, **Unit tests (3.12)**, **Compile + help smoke (macos-latest, 3.11)**, **Compile + help smoke (windows-latest, 3.11)**, **Windows unit tests**, **Windows MSI smoke**, **No build artifacts tracked**.
   - Require linear history.  
   - Block force pushes and branch deletion.  
   - Bypass: none configured in rulesets.

2. **Release branches**  
   - Target: branch pattern `release/*`.  
   - Same rules as above (PR + thread resolution + strict required checks + linear history + no force push + no deletion; self-approval allowed).

## Write access scope

- Rulesets protect branch behavior, but **repository write access** is controlled by repository/org membership and role assignments.
- Keep write access restricted to internal staff by granting write/admin roles only to internal users/teams.

## Branch vs repo deletion

- **Branches (e.g. main)**: The **deletion** rule in these rulesets protects the targeted branches. Only users with bypass permission (e.g. repo admins) can delete `main` or `release/*`.
- **Whole repository**: Branch protection does **not** protect against deleting the entire repo. Limit organization/repository deletion permissions and keep repo admin access narrow.

## Required status check names

- Use check names exactly as they appear on pull requests. In this repo, required checks are:
  - **Analyze (python)**
  - **Unit tests (3.11)**
  - **Unit tests (3.12)**
  - **Compile + help smoke (macos-latest, 3.11)**
  - **Compile + help smoke (windows-latest, 3.11)**
  - **Windows unit tests**
  - **Windows MSI smoke**
  - **No build artifacts tracked**

## Optional: apply via API

From the repo root, with `gh` authenticated:

```bash
REPO="wildfoundry/dataplicity-cli"
CONTEXTS='[
  {"context":"Analyze (python)"},
  {"context":"Unit tests (3.11)"},
  {"context":"Unit tests (3.12)"},
  {"context":"Compile + help smoke (macos-latest, 3.11)"},
  {"context":"Compile + help smoke (windows-latest, 3.11)"},
  {"context":"Windows unit tests"},
  {"context":"Windows MSI smoke"},
  {"context":"No build artifacts tracked"}
]'

# Ruleset: protect main
gh api "repos/${REPO}/rulesets" -X POST -f name="Protect main" \
  -f target=branch \
  -f enforcement=active \
  -F 'conditions[ref_name][include]=refs/heads/main' \
  -f 'rules[0][type]=pull_request' \
  -F 'rules[0][parameters][required_approving_review_count]=0' \
  -F 'rules[0][parameters][dismiss_stale_reviews_on_push]=true' \
  -F 'rules[0][parameters][require_code_owner_review]=false' \
  -F 'rules[0][parameters][require_last_push_approval]=false' \
  -F 'rules[0][parameters][required_review_thread_resolution]=true' \
  -f 'rules[1][type]=required_status_checks' \
  -F 'rules[1][parameters][strict_required_status_checks_policy]=true' \
  -F "rules[1][parameters][required_status_checks]=${CONTEXTS}" \
  -f 'rules[2][type]=required_linear_history' \
  -f 'rules[3][type]=non_fast_forward' \
  -f 'rules[4][type]=deletion'

# Ruleset: protect release/*
gh api "repos/${REPO}/rulesets" -X POST -f name="Protect release branches" \
  -f target=branch \
  -f enforcement=active \
  -F 'conditions[ref_name][include]=refs/heads/release/*' \
  -f 'rules[0][type]=pull_request' \
  -F 'rules[0][parameters][required_approving_review_count]=0' \
  -F 'rules[0][parameters][dismiss_stale_reviews_on_push]=true' \
  -F 'rules[0][parameters][require_code_owner_review]=false' \
  -F 'rules[0][parameters][require_last_push_approval]=false' \
  -F 'rules[0][parameters][required_review_thread_resolution]=true' \
  -f 'rules[1][type]=required_status_checks' \
  -F 'rules[1][parameters][strict_required_status_checks_policy]=true' \
  -F "rules[1][parameters][required_status_checks]=${CONTEXTS}" \
  -f 'rules[2][type]=required_linear_history' \
  -f 'rules[3][type]=non_fast_forward' \
  -f 'rules[4][type]=deletion'
```

If GitHub rejects form-encoded ruleset fields, submit a single JSON body using `.github/ruleset-main.json` and `.github/ruleset-release.json` with `gh api --input`.
