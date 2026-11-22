---
type: sprint_planning
label: Sprint planning
keywords: sprint planning, sprint goal, story points, points, capacity, backlog, commit, velocity, estimate, user story, next sprint, ticket
fields: sprint_goal, committed_stories, capacity
guidance: Fill details.sprint_goal, details.committed_stories (story, owner, estimate) and details.capacity (who is available, holidays, velocity). Put risks to the sprint in risks.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Sprint goal & capacity
**Sprint goal:** {{details.sprint_goal}}  
**Capacity:** {{details.capacity}}

{{/show.type}}
{{#show.type}}
## Committed stories
{{#has.committed_stories}}
| Story | Owner | Estimate |
|---|---|---|
{{#details.committed_stories}}
| {{story}} | {{owner}} | {{estimate}} |
{{/details.committed_stories}}
{{/has.committed_stories}}
{{^has.committed_stories}}
_Not discussed._
{{/has.committed_stories}}

{{/show.type}}
{{> discussion}}
{{> decisions}}
{{> actions}}
{{> risks}}
{{> questions}}
{{> next}}
{{> appendix}}
