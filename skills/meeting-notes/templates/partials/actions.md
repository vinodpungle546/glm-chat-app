{{#show.actions}}
## Action items
{{#has.actions}}
| # | Action | Owner | Due date | Priority | Timestamp |
|---|---|---|---|---|---|
{{#actions}}
| {{n}} | {{action}} | {{owner}} | {{due_date}} | {{priority}} | {{ts}} |
{{/actions}}
{{/has.actions}}
{{^has.actions}}
_None recorded._
{{/has.actions}}
{{#has.follow_up}}

> **Needs follow-up:** {{follow_up_display}}
{{/has.follow_up}}

{{/show.actions}}
