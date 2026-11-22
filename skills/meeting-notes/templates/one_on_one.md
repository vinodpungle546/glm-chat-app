---
type: one_on_one
label: 1:1
keywords: one-on-one, one on one, 1:1, 1-1, one to one, career, growth, how are you feeling, development plan, my goals, your goals, workload, promotion, feedback for you
fields: discussion_points, feedback
guidance: Fill details.discussion_points and details.feedback (from, feedback). Agreed actions go in action_items. Keep personal matters high level unless detail level is Detailed.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Discussion points
{{#has.discussion_points}}
{{#details.discussion_points}}
- {{.}}
{{/details.discussion_points}}
{{/has.discussion_points}}
{{^has.discussion_points}}
_Not discussed._
{{/has.discussion_points}}

{{/show.type}}
{{#show.type}}
## Feedback
{{#has.feedback}}
{{#details.feedback}}
- {{#from}}**{{from}}:** {{/from}}{{feedback}}
{{/details.feedback}}
{{/has.feedback}}
{{^has.feedback}}
_Not discussed._
{{/has.feedback}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> next}}
