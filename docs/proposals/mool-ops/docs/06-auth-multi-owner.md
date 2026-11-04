# 06 · Authentication across several owners

A logical organization often spans a GitHub org plus personal accounts. Locally this is easy: a person's `gh` login reads whatever they can access. A **scheduled run** (CI) is where it gets harder.

| Option | Reaches several owners? | Notes |
|---|---|---|
| CI built-in token (`GITHUB_TOKEN`) | No | Only the repo where the workflow runs |
| Fine-grained personal token | One owner per token | Cannot read another person's personal repos where you are only a collaborator |
| Classic personal token | Yes | Broad scopes, tied to one person; avoid for shared automation |
| **GitHub App (read-only)** | **Yes, one installation per owner** | Recommended. Permissions: metadata, issues, pull requests (read). Each owner installs it; the job mints one token per installation |

## Recommended setup

1. Create a GitHub App with read-only permissions: metadata, issues, pull requests.
2. Install it on each tracked owner (`core` and `affiliated`). **Do not install it on `confidential` owners.**
3. In the scheduled job, mint one installation token per owner and expose it as `GH_TOKEN_<OWNER>` (upper-case, `-` → `_`). The reference implementation picks it up per repo.
4. Store the App's private key only in the data repo's CI secrets.

## Local runs

`gh auth login` is enough. A coordinator only sees what their account can read; the shared data repo is what makes both views consistent.
