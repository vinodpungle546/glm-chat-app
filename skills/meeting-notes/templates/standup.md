---
type: standup
label: Daily stand-up
keywords: stand-up, standup, stand up, daily, yesterday, today i, blockers, blocked, working on today, scrum, quick update
fields: standup_updates
guidance: For each person, fill details.standup_updates with what they did yesterday, plan today, and any blockers. Blockers also go in risks with type Blocker.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Stand-up updates
{{#has.standup_updates}}
| Person | Yesterday | Today | Blockers |
|---|---|---|---|
{{#details.standup_updates}}
| {{person}} | {{yesterday}} | {{today}} | {{blockers}} |
{{/details.standup_updates}}
{{/has.standup_updates}}
{{^has.standup_updates}}
_Not discussed._
{{/has.standup_updates}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> risks}}
{{> questions}}
{{> next}}
{{> appendix}}
