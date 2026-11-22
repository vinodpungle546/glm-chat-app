---
type: release_review
label: Release / go-live review
keywords: release, go-live, go live, go/no-go, go no go, no-go, rollback, roll back, deploy, deployment, cutover, sign-off, sign off, production, readiness, smoke test, release candidate
fields: readiness, go_no_go, sign_offs, rollback_plan
guidance: Fill details.readiness (checklist item, status Ready/Not ready/At risk, owner), details.go_no_go (decision Go/No-go/Conditional go/Pending, conditions, timestamp), details.sign_offs (name, area, status) and details.rollback_plan.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Go / no-go
**Decision:** {{details.go_no_go.decision}}{{#details.go_no_go.conditions}} — {{details.go_no_go.conditions}}{{/details.go_no_go.conditions}}{{#details.go_no_go.ts}} ({{details.go_no_go.ts}}){{/details.go_no_go.ts}}

{{/show.type}}
{{#show.type}}
## Readiness checklist
{{#has.readiness}}
| Item | Status | Owner |
|---|---|---|
{{#details.readiness}}
| {{item}} | {{status}} | {{owner}} |
{{/details.readiness}}
{{/has.readiness}}
{{^has.readiness}}
_Not discussed._
{{/has.readiness}}

{{/show.type}}
{{#show.type}}
## Sign-offs
{{#has.sign_offs}}
| Name | Area | Status |
|---|---|---|
{{#details.sign_offs}}
| {{name}} | {{area}} | {{status}} |
{{/details.sign_offs}}
{{/has.sign_offs}}
{{^has.sign_offs}}
_Not discussed._
{{/has.sign_offs}}

{{/show.type}}
{{#show.type}}
## Rollback plan
{{details.rollback_plan}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> risks}}
{{> questions}}
{{> next}}
{{> appendix}}
