{{#show.next}}
## Next steps & next meeting
{{#next_steps}}
- {{.}}
{{/next_steps}}
{{#next_meeting}}
- **Next meeting:** {{date}}{{#time}}, {{time}}{{/time}}{{#agenda_display}} — agenda: {{agenda_display}}{{/agenda_display}}
{{/next_meeting}}
{{^has.next}}
_No next steps or follow-up meeting mentioned._
{{/has.next}}

{{/show.next}}
