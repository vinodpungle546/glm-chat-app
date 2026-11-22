#!/usr/bin/env python3
"""Generates the synthetic fixture meetings (one per meeting type): <type>.json is the transcript
(transcriber format) and <type>.notes.json is what a well-behaved model returns for it.
All names, companies and content are invented. Run: python tests/fixtures/make_meetings.py"""
import json
from pathlib import Path

OUT = Path(__file__).parent / "meetings"
M = {}

def meeting(t, title, people, lines, notes, step=20):
    spk = {name: f"SPEAKER_{i:02d}" for i, name in enumerate(people)}
    segs = [{"speaker": spk[n], "start": float(i * step), "end": float(i * step + step - 2), "text": text}
            for i, (n, text) in enumerate(lines)]
    M[t] = ({"id": f"fx-{t.replace('_', '-')}", "title": title, "source_name": f"{t}.m4a",
             "source_path": f"/fake/Meetings/{t}.m4a", "source_type": "audio", "created_at": "2026-11-18T10:00:00",
             "duration": float(len(lines) * step), "language": "en", "diarization": True,
             "speakers": list(spk.values()), "speaker_names": {v: k for k, v in spk.items()},
             "segments": segs, "processing_seconds": 30, "timestamps": True}, notes)

def ts(i, step=20):
    s = i * step
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

meeting("general", "Office move planning", ["Nadia", "Tom", "Leah"], [
    ("Nadia", "Thanks for joining. Today we need to agree the date for the office move and who handles the furniture vendor."),
    ("Tom", "The landlord confirmed the new floor is ready from the first of December."),
    ("Leah", "Then I suggest we move on Saturday the fifth so nobody loses a working day."),
    ("Nadia", "Agreed, we will move on the fifth of December."),
    ("Tom", "I'll book the furniture vendor by Friday."),
    ("Leah", "I can send the seating plan to everyone tomorrow."),
    ("Nadia", "One open question is whether the parking permits transfer automatically."),
    ("Tom", "I don't know, someone should check with facilities."),
], {"meeting": {"title": "Office move planning", "project": ""},
    "tldr": ["Office move agreed for Saturday 5 Dec 2026", "Tom books the furniture vendor; Leah shares the seating plan",
             "Parking permit transfer is still unclear"],
    "topics": [{"title": "Move date", "points": [{"text": "New floor is ready from 1 December", "speaker": "Tom", "timestamp": ts(1)},
                                                  {"text": "Moving on a Saturday avoids losing a working day", "speaker": "Leah", "timestamp": ts(2)}]}],
    "decisions": [{"decision": "Move the office on Saturday 5 December", "made_by": "Nadia", "rationale": "Nobody loses a working day", "timestamp": ts(3)}],
    "action_items": [{"action": "Book the furniture vendor", "owner": "Tom", "due_date": "Friday", "priority": "High", "timestamp": ts(4)},
                     {"action": "Send the seating plan to everyone", "owner": "Leah", "due_date": "tomorrow", "priority": "Medium", "timestamp": ts(5)},
                     {"action": "Check with facilities whether parking permits transfer", "owner": "", "due_date": "", "priority": "", "timestamp": ts(7)}],
    "risks": [], "open_questions": ["Do parking permits transfer automatically?"], "next_steps": [],
    "next_meeting": {"date": "", "time": "", "agenda": []},
    "quotes": [{"speaker": "Nadia", "quote": "Agreed, we will move on the fifth of December.", "timestamp": ts(3)}]})

meeting("standup", "Daily stand-up — Payments squad", ["Priya", "Owen", "Mai"], [
    ("Priya", "Morning everyone, quick stand-up. Owen, you first."),
    ("Owen", "Yesterday I finished the regression run on Android. Today I'm testing the refund flow on iOS."),
    ("Owen", "I'm blocked on the iOS test devices, they haven't arrived yet."),
    ("Priya", "I'll chase procurement about the test devices today."),
    ("Mai", "Yesterday I merged the refund endpoint fix. Today I'll deploy it to staging."),
    ("Mai", "No blockers for me."),
    ("Priya", "Great. Owen, please retest refunds once Mai's fix is on staging, by Friday."),
    ("Owen", "Will do."),
], {"meeting": {"title": "Daily stand-up — Payments squad", "project": "Payments"},
    "tldr": ["Android regression is done; iOS refund testing is blocked on test devices",
             "Refund endpoint fix merged and going to staging today", "Owen retests refunds by Friday"],
    "topics": [],
    "decisions": [],
    "action_items": [{"action": "Chase procurement about the iOS test devices", "owner": "Priya", "due_date": "today", "priority": "High", "timestamp": ts(3)},
                     {"action": "Deploy the refund endpoint fix to staging", "owner": "Mai", "due_date": "today", "priority": "High", "timestamp": ts(4)},
                     {"action": "Retest refunds on staging", "owner": "Owen", "due_date": "Friday", "priority": "Medium", "timestamp": ts(6)}],
    "risks": [{"item": "iOS test devices have not arrived", "type": "Blocker", "impact": "iOS refund testing can't start", "owner": "Priya", "mitigation": "Chase procurement"}],
    "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"standup_updates": [
        {"person": "Owen", "yesterday": "Finished Android regression run", "today": "Test the iOS refund flow", "blockers": "iOS test devices not arrived"},
        {"person": "Mai", "yesterday": "Merged the refund endpoint fix", "today": "Deploy the fix to staging", "blockers": "None"}]}})

meeting("sprint_planning", "Sprint 24 planning", ["Raj", "Elena", "Ben"], [
    ("Raj", "Welcome to sprint planning. The sprint goal I'd propose is: customers can pay with saved cards."),
    ("Elena", "Capacity is lower this sprint, I'm on holiday Thursday and Friday, so about thirty story points total."),
    ("Ben", "The saved cards API story is eight points, I can take it."),
    ("Elena", "I'll take the checkout UI story, five points."),
    ("Raj", "Let's commit to those two plus the card-expiry email, three points, owned by Ben."),
    ("Ben", "The risk is the payment gateway sandbox, it has been unstable all week."),
    ("Raj", "Then I'll ask the gateway vendor for a stable sandbox by Monday."),
    ("Elena", "Agreed, the sprint goal is saved-card payments."),
], {"meeting": {"title": "Sprint 24 planning", "project": ""},
    "tldr": ["Sprint goal: customers can pay with saved cards", "Committed 16 points across three stories; capacity about 30 points",
             "Gateway sandbox instability is the main risk"],
    "topics": [], "decisions": [{"decision": "Sprint goal is saved-card payments", "made_by": "Raj", "rationale": "", "timestamp": ts(7)},
                                {"decision": "Commit to the saved cards API, checkout UI and card-expiry email stories", "made_by": "Raj", "rationale": "", "timestamp": ts(4)}],
    "action_items": [{"action": "Ask the gateway vendor for a stable sandbox", "owner": "Raj", "due_date": "by Monday", "priority": "High", "timestamp": ts(6)}],
    "risks": [{"item": "Payment gateway sandbox is unstable", "type": "Risk", "impact": "Could block API testing", "owner": "Raj", "mitigation": "Ask the vendor for a stable sandbox"}],
    "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"sprint_goal": "Customers can pay with saved cards", "capacity": "About 30 story points; Elena on holiday Thursday and Friday",
                "committed_stories": [{"story": "Saved cards API", "owner": "Ben", "estimate": "8 points"},
                                      {"story": "Checkout UI", "owner": "Elena", "estimate": "5 points"},
                                      {"story": "Card-expiry email", "owner": "Ben", "estimate": "3 points"}]}})

meeting("retrospective", "Sprint 23 retrospective", ["Sam", "Ivy", "Kofi"], [
    ("Sam", "Let's start the retro. What went well this sprint?"),
    ("Ivy", "Pairing on the search feature went really well, we shipped it two days early."),
    ("Kofi", "The new code review checklist caught three bugs before release."),
    ("Sam", "And what didn't go well?"),
    ("Ivy", "Standups ran long, often thirty minutes."),
    ("Kofi", "We kept getting surprise requests from sales mid-sprint."),
    ("Sam", "Action: I'll timebox standups to fifteen minutes from Monday."),
    ("Kofi", "I'll set up an intake form for sales requests this week."),
], {"meeting": {"title": "Sprint 23 retrospective", "project": ""},
    "tldr": ["Pairing and the review checklist worked well", "Long standups and mid-sprint sales requests were problems",
             "Standups timeboxed to 15 minutes; sales intake form coming"],
    "topics": [], "decisions": [{"decision": "Timebox standups to fifteen minutes", "made_by": "Sam", "rationale": "Standups ran long", "timestamp": ts(6)}],
    "action_items": [{"action": "Timebox standups to fifteen minutes", "owner": "Sam", "due_date": "Monday", "priority": "Medium", "timestamp": ts(6)},
                     {"action": "Set up an intake form for sales requests", "owner": "Kofi", "due_date": "end of the week", "priority": "Medium", "timestamp": ts(7)}],
    "risks": [], "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"went_well": ["Pairing on search shipped it two days early", "Code review checklist caught three bugs"],
                "didnt_go_well": ["Standups ran about thirty minutes", "Surprise sales requests mid-sprint"]}})

meeting("status_review", "Atlas programme steering review", ["Grace", "Hugo", "Wen"], [
    ("Grace", "Overall status for Atlas is amber this month, mainly because data migration slipped."),
    ("Hugo", "Data migration milestone moves from the first to the fifteenth of December."),
    ("Wen", "UAT is still on track for January."),
    ("Grace", "We are assuming the vendor delivers the API keys next week."),
    ("Hugo", "The migration depends on the security review finishing."),
    ("Grace", "I'll escalate the security review delay to the CIO."),
    ("Wen", "Decision then: we keep the January go-live date."),
    ("Grace", "Yes, January go-live stays."),
], {"meeting": {"title": "Atlas programme steering review", "project": "Atlas"},
    "tldr": ["Atlas is Amber: data migration slipped to 15 Dec", "UAT on track for January; go-live date kept",
             "Security review delay escalated to the CIO"],
    "topics": [], "decisions": [{"decision": "Keep the January go-live date", "made_by": "Grace", "rationale": "UAT is on track", "timestamp": ts(7)}],
    "action_items": [{"action": "Escalate the security review delay to the CIO", "owner": "Grace", "due_date": "", "priority": "High", "timestamp": ts(5)}],
    "risks": [{"item": "Data migration depends on the security review", "type": "Dependency", "impact": "Migration milestone could slip again", "owner": "Hugo", "mitigation": "Escalate to the CIO"}],
    "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"rag_status": {"overall": "Amber", "reason": "Data migration slipped"},
                "milestones": [{"milestone": "Data migration", "date": "15 Dec 2026", "status": "Slipped"},
                               {"milestone": "UAT", "date": "January", "status": "On track"}],
                "assumptions": ["Vendor delivers the API keys next week"], "escalations": ["Security review delay escalated to the CIO"]}})

meeting("client_call", "Northwind weekly client call", ["Carla", "Dev", "Ana"], [
    ("Carla", "From our side at Northwind, we need the invoice export in CSV, that's the top request."),
    ("Dev", "We can deliver the CSV export by the end of next week."),
    ("Carla", "We'd also like a demo of the approval workflow."),
    ("Ana", "I'll schedule the approval workflow demo for Tuesday."),
    ("Carla", "Is the single sign-on integration still planned for this quarter?"),
    ("Dev", "We need to confirm that with our architect and come back to you."),
    ("Ana", "I'll send you the meeting recap today."),
], {"meeting": {"title": "Northwind weekly client call", "project": "Northwind"},
    "tldr": ["Northwind's top request is a CSV invoice export", "CSV export promised by end of next week; approval workflow demo on Tuesday",
             "Single sign-on timing to be confirmed"],
    "topics": [], "decisions": [],
    "action_items": [{"action": "Schedule the approval workflow demo", "owner": "Ana", "due_date": "Tuesday", "priority": "Medium", "timestamp": ts(3)},
                     {"action": "Confirm the single sign-on timeline with the architect", "owner": "Dev", "due_date": "", "priority": "Medium", "timestamp": ts(5)},
                     {"action": "Send the meeting recap to Northwind", "owner": "Ana", "due_date": "today", "priority": "Low", "timestamp": ts(6)}],
    "risks": [], "open_questions": ["Is single sign-on still planned for this quarter?"], "next_steps": [],
    "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"client_requests": ["Invoice export in CSV", "Demo of the approval workflow", "Single sign-on this quarter"],
                "our_commitments": [{"commitment": "Deliver the CSV invoice export", "owner": "Dev", "due_date": "end of next week"},
                                    {"commitment": "Approval workflow demo", "owner": "Ana", "due_date": "Tuesday"}]}})

meeting("release_review", "Release 4.2 go/no-go", ["Cian", "Owen", "Mai"], [
    ("Cian", "This is the go/no-go for release 4.2. Let's walk the readiness checklist."),
    ("Owen", "Regression is complete and passed, QA signs off."),
    ("Mai", "Database migration scripts are ready and tested on staging."),
    ("Cian", "Release notes are not ready yet, I'll finish them by two o'clock."),
    ("Mai", "Rollback plan: we redeploy 4.1 and restore the database snapshot taken before release."),
    ("Cian", "Decision: conditional go, provided the release notes are done before the deployment."),
    ("Owen", "I'll run the smoke test right after the deployment."),
], {"meeting": {"title": "Release 4.2 go/no-go", "project": ""},
    "tldr": ["Conditional go for release 4.2", "Regression passed and migrations are ready", "Release notes must be finished before deployment"],
    "topics": [], "decisions": [{"decision": "Conditional go for release 4.2", "made_by": "Cian", "rationale": "Release notes must be done before the deployment", "timestamp": ts(5)}],
    "action_items": [{"action": "Finish the release notes", "owner": "Cian", "due_date": "today", "priority": "High", "timestamp": ts(3)},
                     {"action": "Run the smoke test after the deployment", "owner": "Owen", "due_date": "today", "priority": "High", "timestamp": ts(6)}],
    "risks": [], "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"readiness": [{"item": "Regression", "status": "Ready", "owner": "Owen"},
                              {"item": "Database migration scripts", "status": "Ready", "owner": "Mai"},
                              {"item": "Release notes", "status": "Not ready", "owner": "Cian"}],
                "go_no_go": {"decision": "Conditional go", "conditions": "Release notes done before deployment", "timestamp": ts(5)},
                "sign_offs": [{"name": "Owen", "area": "QA", "status": "Signed off"}],
                "rollback_plan": "Redeploy 4.1 and restore the pre-release database snapshot"}})

meeting("incident", "Checkout outage postmortem", ["Lena", "Marco", "Yusuf"], [
    ("Lena", "This is the postmortem for Tuesday's checkout outage."),
    ("Marco", "At 14:02 the alert fired for checkout errors, and we paged the on-call engineer."),
    ("Yusuf", "At 14:20 we rolled back the config change and checkout recovered at 14:25."),
    ("Lena", "Impact: about two thousand customers couldn't pay for twenty-three minutes."),
    ("Marco", "The root cause was a config change that set the payment timeout to zero."),
    ("Yusuf", "I'll add validation so the timeout can't be set below one second, by next Friday."),
    ("Lena", "And I'll update the change review checklist to include config values."),
], {"meeting": {"title": "Checkout outage postmortem", "project": ""},
    "tldr": ["Checkout was down 23 minutes; about 2,000 customers affected", "Root cause: a config change set the payment timeout to zero",
             "Timeout validation and a checklist update agreed"],
    "topics": [], "decisions": [],
    "action_items": [{"action": "Add validation so the payment timeout can't be below one second", "owner": "Yusuf", "due_date": "next Friday", "priority": "High", "timestamp": ts(5)},
                     {"action": "Update the change review checklist to include config values", "owner": "Lena", "due_date": "", "priority": "Medium", "timestamp": ts(6)}],
    "risks": [], "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"impact": "About two thousand customers couldn't pay for twenty-three minutes",
                "root_cause": "A config change set the payment timeout to zero",
                "incident_timeline": [{"time": "14:02", "event": "Alert fired for checkout errors; on-call paged"},
                                      {"time": "14:20", "event": "Config change rolled back"},
                                      {"time": "14:25", "event": "Checkout recovered"}]}})

meeting("one_on_one", "1:1 Zoe / Arjun", ["Zoe", "Arjun"], [
    ("Zoe", "How are you feeling about your workload this month?"),
    ("Arjun", "It's heavy, the reporting work takes most of my week."),
    ("Zoe", "Feedback for you: your incident write-up last week was really clear."),
    ("Arjun", "Thanks. For my growth I'd like to lead a small project next quarter."),
    ("Zoe", "Good, I'll look for a project you can lead by the end of the month."),
    ("Arjun", "I'll automate the weekly report so it takes less time."),
], {"meeting": {"title": "1:1 Zoe / Arjun", "project": ""},
    "tldr": ["Arjun's workload is heavy because of reporting", "Positive feedback on the incident write-up", "Arjun wants to lead a project next quarter"],
    "topics": [], "decisions": [],
    "action_items": [{"action": "Find a project Arjun can lead", "owner": "Zoe", "due_date": "end of the month", "priority": "Medium", "timestamp": ts(4)},
                     {"action": "Automate the weekly report", "owner": "Arjun", "due_date": "", "priority": "Medium", "timestamp": ts(5)}],
    "risks": [], "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"discussion_points": ["Workload: reporting takes most of the week", "Growth: lead a small project next quarter"],
                "feedback": [{"from": "Zoe", "feedback": "The incident write-up last week was really clear"}]}})

meeting("interview", "Interview — QA engineer candidate", ["Helen", "Jonas", "Rita"], [
    ("Helen", "Thanks for coming in, Rita. Tell me about your background in test automation."),
    ("Rita", "I've built Playwright test suites for two years, mostly for payment apps."),
    ("Jonas", "Walk me through how you'd test a refund flow."),
    ("Rita", "I'd start with the API contract tests, then end-to-end tests for the happy path and the edge cases like partial refunds."),
    ("Jonas", "Strong answer on test design. I'm less sure about her experience with mobile testing."),
    ("Helen", "Agreed. My recommendation is to move her to the final round."),
    ("Jonas", "I'll send the feedback form to recruiting today."),
], {"meeting": {"title": "Interview — QA engineer candidate", "project": ""},
    "tldr": ["Rita has two years of Playwright automation for payment apps", "Strong test design; mobile testing experience unclear", "Recommended for the final round"],
    "topics": [], "decisions": [{"decision": "Move Rita to the final round", "made_by": "Helen", "rationale": "Strong test design", "timestamp": ts(5)}],
    "action_items": [{"action": "Send the feedback form to recruiting", "owner": "Jonas", "due_date": "today", "priority": "Medium", "timestamp": ts(6)}],
    "risks": [], "open_questions": [], "next_steps": [], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"candidate": "Rita", "position": "QA engineer",
                "competencies": [{"competency": "Test design", "assessment": "Strong", "evidence": "Layered plan for testing a refund flow"},
                                 {"competency": "Mobile testing", "assessment": "Unclear", "evidence": "Not covered"}],
                "strengths": ["Two years of Playwright automation", "Clear test design"], "concerns": ["Mobile testing experience"],
                "recommendation": "Move to the final round"}})

meeting("brainstorm", "Onboarding ideas workshop", ["Tara", "Luis", "Mei"], [
    ("Tara", "Let's brainstorm ideas to improve onboarding. What if we gave new joiners a buddy?"),
    ("Luis", "Idea: a welcome video from the founders."),
    ("Mei", "Could we automate laptop setup so it's ready on day one?"),
    ("Luis", "Another idea, a first-week checklist in the app."),
    ("Tara", "Let's vote. The shortlist is the buddy scheme and automated laptop setup."),
    ("Mei", "I'll write a one-page proposal for laptop automation by next Wednesday."),
], {"meeting": {"title": "Onboarding ideas workshop", "project": ""},
    "tldr": ["Four onboarding ideas raised", "Shortlist: buddy scheme and automated laptop setup", "Mei drafts a laptop automation proposal"],
    "topics": [], "decisions": [{"decision": "Shortlist the buddy scheme and automated laptop setup", "made_by": "Tara", "rationale": "Vote", "timestamp": ts(4)}],
    "action_items": [{"action": "Write a one-page proposal for laptop automation", "owner": "Mei", "due_date": "next Wednesday", "priority": "Medium", "timestamp": ts(5)}],
    "risks": [], "open_questions": [], "next_steps": ["Review the laptop automation proposal"], "next_meeting": {"date": "", "time": "", "agenda": []}, "quotes": [],
    "details": {"idea_themes": [{"theme": "People", "ideas": ["Buddy for new joiners", "Welcome video from the founders"]},
                                {"theme": "Tools", "ideas": ["Automated laptop setup", "First-week checklist in the app"]}],
                "shortlisted_ideas": ["Buddy scheme", "Automated laptop setup"]}})

for t, (transcript, notes) in M.items():
    (OUT / f"{t}.json").write_text(json.dumps(transcript, indent=1, ensure_ascii=False))
    (OUT / f"{t}.notes.json").write_text(json.dumps(notes, indent=1, ensure_ascii=False))
print(len(M), "meetings")
