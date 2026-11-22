---
type: retrospective
label: Retrospective
keywords: retro, retrospective, went well, didn't go well, did not go well, went wrong, start doing, stop doing, keep doing, improve, lessons learned, kudos
fields: went_well, didnt_go_well
guidance: Fill details.went_well and details.didnt_go_well as short bullets. Improvement actions go in action_items.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## What went well
{{#has.went_well}}
{{#details.went_well}}
- {{.}}
{{/details.went_well}}
{{/has.went_well}}
{{^has.went_well}}
_Not discussed._
{{/has.went_well}}

{{/show.type}}
{{#show.type}}
## What didn't go well
{{#has.didnt_go_well}}
{{#details.didnt_go_well}}
- {{.}}
{{/details.didnt_go_well}}
{{/has.didnt_go_well}}
{{^has.didnt_go_well}}
_Not discussed._
{{/has.didnt_go_well}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> questions}}
{{> next}}
{{> appendix}}
