---
type: brainstorm
label: Brainstorm / workshop
keywords: brainstorm, ideas, idea, what if, could we, how might we, workshop, vote, shortlist, sticky, theme, explore, wild idea, dot vote
fields: idea_themes, shortlisted_ideas
guidance: Fill details.idea_themes (theme with its ideas) and details.shortlisted_ideas. Next steps go in next_steps and action_items.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Ideas by theme
{{#has.idea_themes}}
{{#details.idea_themes}}
**{{theme}}**
{{#ideas}}
- {{.}}
{{/ideas}}

{{/details.idea_themes}}
{{/has.idea_themes}}
{{^has.idea_themes}}
_Not discussed._
{{/has.idea_themes}}

{{/show.type}}
{{#show.type}}
## Shortlisted ideas
{{#has.shortlisted_ideas}}
{{#details.shortlisted_ideas}}
- {{.}}
{{/details.shortlisted_ideas}}
{{/has.shortlisted_ideas}}
{{^has.shortlisted_ideas}}
_Not discussed._
{{/has.shortlisted_ideas}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> questions}}
{{> next}}
{{> appendix}}
