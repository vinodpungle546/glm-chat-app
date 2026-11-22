---
type: incident
label: Incident / postmortem
keywords: incident, outage, postmortem, post-mortem, post mortem, root cause, rca, sev1, sev 1, sev2, severity, pager, paged, alert fired, downtime, customers affected, hotfix
fields: incident_timeline, impact, root_cause
guidance: Fill details.incident_timeline (time, event), details.impact and details.root_cause. Corrective actions go in action_items. Blameless tone: describe systems and decisions, not individual fault.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Impact
{{details.impact}}

{{/show.type}}
{{#show.type}}
## Timeline
{{#has.incident_timeline}}
| Time | Event |
|---|---|
{{#details.incident_timeline}}
| {{time}} | {{event}} |
{{/details.incident_timeline}}
{{/has.incident_timeline}}
{{^has.incident_timeline}}
_Not discussed._
{{/has.incident_timeline}}

{{/show.type}}
{{#show.type}}
## Root cause
{{details.root_cause}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> risks}}
{{> questions}}
{{> next}}
{{> appendix}}
