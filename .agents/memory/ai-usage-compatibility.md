---
name: AI usage compatibility
description: Why older daily AI usage records must not replace the request-level ledger
---

Preserve the request-level AI usage ledger as the authoritative record for new paid calls. If importing a prior snapshot that kept only daily token totals, retain those daily records separately instead of rewriting the request ledger or pretending the two schemas are interchangeable.

**Why:** Daily aggregates cannot reconstruct the paid responses rejected by later validation or reliably allocate requests across groups. Losing those records makes the spending-limit check misleading. Historical daily amounts can only be estimated using the old snapshot's rate assumptions; they are not a provider invoice.

**How to apply:** For future changes to AI spending controls or migrations, keep both historical daily totals and new request rows visible to the budget check, and explicitly identify any remaining uncertainty about older charges.