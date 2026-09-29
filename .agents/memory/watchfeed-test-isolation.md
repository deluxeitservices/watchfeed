---
name: Watchfeed test isolation
description: Why new pipeline tests must establish their own database preconditions
---

New pipeline tests should create or enable the group state they require, rather than relying on another test to have run first.

**Why:** A newly added test passed in the full suite but failed when selected alone because earlier tests had prepared its group in a shared fixture. This can mask fragile behavior during focused regression checks.

**How to apply:** When adding a pipeline test that uses a real test database, establish its own essential group and message preconditions, then check the test in isolation as well as in the suite.