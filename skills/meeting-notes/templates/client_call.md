---
type: client_call
label: Client call
keywords: client, customer, your team, our side, on our end, requirement, deliverable, contract, invoice, sow, statement of work, proposal, demo, we will send you, you asked
fields: client_requests, our_commitments
guidance: Fill details.client_requests (what the client asked for) and details.our_commitments (what our side promised: commitment, owner, due_date). Follow-ups go in action_items; open items in open_questions.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Client requests
{{#has.client_requests}}
{{#details.client_requests}}
- {{.}}
{{/details.client_requests}}
{{/has.client_requests}}
{{^has.client_requests}}
_Not discussed._
{{/has.client_requests}}

{{/show.type}}
{{#show.type}}
## Commitments made by our side
{{#has.our_commitments}}
| Commitment | Owner | Due date |
|---|---|---|
{{#details.our_commitments}}
| {{commitment}} | {{owner}} | {{due_date}} |
{{/details.our_commitments}}
{{/has.our_commitments}}
{{^has.our_commitments}}
_Not discussed._
{{/has.our_commitments}}

{{/show.type}}
{{> discussion}}
{{> decisions}}
{{> actions}}
{{> risks}}
{{> questions}}
{{> next}}
{{> appendix}}
