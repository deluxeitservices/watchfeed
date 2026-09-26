---
name: Alert queue limits
description: Why notification queue batching must exclude completed markers before limiting work.
---

When batching persistent alert deliveries, filter for pending and due work before applying a numerical batch limit.

**Why:** A review caught a dispatcher that selected the first 100 keys before filtering status. Completed markers remained in storage for days, so enough earlier keys would permanently starve later notifications even while the worker continued running normally.

**How to apply:** Whenever alert retries, digest jobs, or their retention rules change, test with more completed markers than the batch limit ahead of a due notification. A limit must apply to actionable jobs, not all historical rows.