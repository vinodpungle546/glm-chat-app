{{#show.risks}}
## Risks, issues & blockers
{{#has.risks}}
| Item | Type | Impact | Owner | Mitigation |
|---|---|---|---|---|
{{#risks}}
| {{item}} | {{type}} | {{impact}} | {{owner}} | {{mitigation}} |
{{/risks}}
{{/has.risks}}
{{^has.risks}}
_None raised._
{{/has.risks}}

{{/show.risks}}
