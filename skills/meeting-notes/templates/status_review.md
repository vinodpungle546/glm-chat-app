---
type: status_review
label: Status / steering review
keywords: status, steering, rag, amber, red status, green, milestone, on track, off track, escalate, escalation, budget, raid, programme, roadmap, stakeholder, overall status
fields: rag_status, milestones, assumptions, escalations
guidance: Fill details.rag_status (overall Red/Amber/Green and reason), details.milestones (milestone, date, status), details.assumptions and details.escalations. Risks, issues and dependencies go in risks (the RAID log).
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Overall status
**RAG:** {{details.rag_status.overall}}{{#details.rag_status.reason}} — {{details.rag_status.reason}}{{/details.rag_status.reason}}

{{/show.type}}
{{#show.type}}
## Milestones
{{#has.milestones}}
| Milestone | Date | Status |
|---|---|---|
{{#details.milestones}}
| {{milestone}} | {{date}} | {{status}} |
{{/details.milestones}}
{{/has.milestones}}
{{^has.milestones}}
_Not discussed._
{{/has.milestones}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> risks}}
{{#show.type}}
## Assumptions (RAID)
{{#has.assumptions}}
{{#details.assumptions}}
- {{.}}
{{/details.assumptions}}
{{/has.assumptions}}
{{^has.assumptions}}
_Not discussed._
{{/has.assumptions}}

{{/show.type}}
{{#show.type}}
## Escalations
{{#has.escalations}}
{{#details.escalations}}
- {{.}}
{{/details.escalations}}
{{/has.escalations}}
{{^has.escalations}}
_Not discussed._
{{/has.escalations}}

{{/show.type}}
{{> questions}}
{{> next}}
{{> appendix}}
