# Branch and tag protection

This repository uses GitHub repository rulesets. Public contributors may open pull requests from forks; they do not receive write access.

## Access

- Organisation default repository permission: **none**
- Write/maintain access is limited to internal staff teams (currently `@wildfoundry/dataplicity-web-developers` and related Dataplicity developer teams)
- No outside collaborators
- Direct pushes to protected refs are blocked by rulesets (no bypass actors)

## Protect main / release/*

`.github/ruleset-main.json` and `.github/ruleset-release.json` require:

- Pull request before merge
- At least one approving review, including a CODEOWNERS review
- Stale review dismissal and last-push approval
- Resolved review threads
- Required CI status checks
- Linear history
- No force pushes and no branch deletion

## Protect version tags

`.github/ruleset-tags.json` protects `v*` tags from deletion and force-updates.

## Apply or refresh via API

```bash
REPO=wildfoundry/dataplicity-cli
# Update existing Protect main ruleset ID as needed
gh api --method PUT "repos/${REPO}/rulesets/<id>" --input .github/ruleset-main.json
gh api --method PUT "repos/${REPO}/rulesets/<id>" --input .github/ruleset-release.json
gh api --method POST "repos/${REPO}/rulesets" --input .github/ruleset-tags.json
```
