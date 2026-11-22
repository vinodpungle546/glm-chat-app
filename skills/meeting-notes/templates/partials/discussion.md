{{#show.discussion}}
## Agenda & discussion
{{#topics}}
### {{title}}
{{#points}}
- {{#speaker}}**{{speaker}}:** {{/speaker}}{{text}}{{#ts}} ({{ts}}){{/ts}}
{{/points}}

{{/topics}}
{{^topics}}
_No discussion topics recorded._

{{/topics}}
{{/show.discussion}}
