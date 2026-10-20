# Rules of engagement
- Stay strictly inside the scope the Engagement Owner defined. If unsure whether something is in scope, escalate `out_of_scope`; do not proceed.
- No destructive actions: no deleting, no exploiting against live systems, no data exfiltration. Escalate `destructive` first.
- Every finding must include evidence (file:line, command output, or a reproduction) and a severity with reasoning.
- The target is read-only for reviewers. Write only under your own owned directory.
- Never verify your own work.
