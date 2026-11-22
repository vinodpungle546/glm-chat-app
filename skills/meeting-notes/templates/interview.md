---
type: interview
label: Interview
keywords: interview, candidate, tell me about yourself, your background, experience with, walk me through, hire, hiring, notice period, why do you want, any questions for us
fields: candidate, position, competencies, strengths, concerns, recommendation
guidance: Fill details.candidate, details.position, details.competencies (competency, assessment, evidence), details.strengths, details.concerns and details.recommendation (only if the interviewers stated one; otherwise 'Not stated'). Assess only what was said; no protected characteristics.
---
{{> header}}
{{> tldr}}
{{#show.type}}
## Candidate
**Candidate:** {{details.candidate}}{{#details.position}} · **Position:** {{details.position}}{{/details.position}}

{{/show.type}}
{{#show.type}}
## Competencies assessed
{{#has.competencies}}
| Competency | Assessment | Evidence |
|---|---|---|
{{#details.competencies}}
| {{competency}} | {{assessment}} | {{evidence}} |
{{/details.competencies}}
{{/has.competencies}}
{{^has.competencies}}
_Not discussed._
{{/has.competencies}}

{{/show.type}}
{{#show.type}}
## Strengths
{{#has.strengths}}
{{#details.strengths}}
- {{.}}
{{/details.strengths}}
{{/has.strengths}}
{{^has.strengths}}
_Not discussed._
{{/has.strengths}}

{{/show.type}}
{{#show.type}}
## Concerns
{{#has.concerns}}
{{#details.concerns}}
- {{.}}
{{/details.concerns}}
{{/has.concerns}}
{{^has.concerns}}
_Not discussed._
{{/has.concerns}}

{{/show.type}}
{{#show.type}}
## Recommendation
{{details.recommendation}}

{{/show.type}}
{{> decisions}}
{{> actions}}
{{> next}}
