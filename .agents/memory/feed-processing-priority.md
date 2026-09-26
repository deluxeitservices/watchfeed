---
name: Feed processing priority
description: The agreed tradeoff between fresh offers and an older AI parsing backlog.
---

Favor processing newly received WhatsApp messages before older pending messages when the parsing queue backs up. Keep older pending messages for later processing; do not silently delete or skip them.

**Why:** The user explicitly prioritized seeing fresh offers sooner after a paid AI parsing backlog delayed the live feed. They accepted that older messages may be delayed and that processing still incurs AI charges.

**How to apply:** Preserve newest-first scheduling in queue changes unless the user asks otherwise. Treat skipping old messages, bulk retrying failed messages, or imposing an AI budget as separate decisions that need their own cost and data-loss tradeoffs explained.