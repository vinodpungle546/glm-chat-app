{{#show.decisions}}
## Decisions
{{#has.decisions}}
| Decision | Made by | Rationale | Timestamp |
|---|---|---|---|
{{#decisions}}
| {{decision}} | {{made_by}} | {{rationale}} | {{ts}} |
{{/decisions}}
{{/has.decisions}}
{{^has.decisions}}
_None recorded._
{{/has.decisions}}

{{/show.decisions}}
