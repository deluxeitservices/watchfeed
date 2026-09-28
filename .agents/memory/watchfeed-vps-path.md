---
name: Watchfeed VPS repository path
description: User-supplied path for the existing Watchfeed Git clone on the VPS
---

The user says the existing Watchfeed repository on their VPS is at `/opt/watchfeed-repo`.

**Why:** Future pull-and-upgrade instructions can use the actual path instead of a placeholder.

**How to apply:** Start example VPS commands with `cd /opt/watchfeed-repo` when relevant, but verify the live checkout and Compose setup before prescribing changes. This is context, not permission to access or alter the VPS.