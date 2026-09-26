---
name: GitHub shell authentication
description: How to distinguish connected GitHub API permissions from Git CLI authentication in Replit.
---

GitHub integration authorization and Git authentication in the Replit Shell are separate. A connection with repository push permission can coexist with a failing `git push`.

**Why:** In this workspace, the connected GitHub API reported push permission, while the Shell's Git authentication still returned an invalid-credential error. Attaching the existing connection did not repair Shell authentication.

**How to apply:** Diagnose repository access and Shell authentication separately before concluding that the repository denies write access. For Shell pushes, use Replit Git Providers reauthorization or a repository-scoped credential supplied through the secure secrets flow; never embed a token in a remote URL or tracked files.