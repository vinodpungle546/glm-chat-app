"""Synthetic meetings where the speakers' names can be inferred from what is said (introductions,
hand-overs, thanks), plus speakers who are never named. For measuring the speaker-identification
pass (scripts/benchmark_screen_names.py). No real people or meetings."""

from __future__ import annotations

MEETINGS = [
    {"title": "Daily standup", "truth": ["Priya", "Daniel", "Mei", "Tomás"], "lines": [
        (0, "Morning everyone, let's get started. Daniel, do you want to go first?"),
        (1, "Sure. Yesterday I finished the refund API and today I'm writing the integration tests."),
        (0, "Great, thanks Daniel. Mei, how about you?"),
        (2, "I'm still on the Android crash. I found the cause, it's the image cache, and I'll ship a fix today."),
        (0, "Nice. Tomás, anything blocking you?"),
        (3, "No blockers. The release notes are drafted and I'll send them to Priya for review after this call."),
        (0, "Perfect, I'll review them this afternoon. Let's wrap up."),
    ]},
    {"title": "Client call", "truth": ["Carla", "Dev", "Ana"], "lines": [
        (0, "Hi both, this is Carla from Northwind. Thanks for making time."),
        (1, "Hello Carla, I'm Dev, the delivery lead on your account."),
        (2, "And I'm Ana, I handle onboarding for new teams."),
        (0, "Our top request is the invoice export in CSV."),
        (1, "We can deliver the CSV export by the end of next week."),
        (2, "I'll send you the onboarding checklist today."),
        (0, "Thanks Ana, that helps a lot."),
    ]},
    {"title": "One-on-one", "truth": ["Sofia", "Owen"], "lines": [
        (0, "Hey Owen, how was your week?"),
        (1, "Pretty good. The regression suite is finally stable."),
        (0, "That's great to hear. Anything you need from me?"),
        (1, "Just a decision on the test devices budget. Thanks for asking, Sofia."),
        (0, "I'll get you an answer by Friday."),
    ]},
    {"title": "Incident review", "truth": ["Lukas", "Aisha", None], "lines": [
        (0, "Okay, I'm Lukas, I was on call last night. The payments service went down at 2 a.m."),
        (1, "Lukas, did the rollback fix it straight away?"),
        (0, "Mostly. Latency stayed high for another twenty minutes."),
        (2, "From the database side we saw connection pool exhaustion."),
        (1, "This is Aisha, by the way, for anyone who joined late. I'll write up the timeline."),
        (2, "I'll add the pool metrics to the dashboard."),
    ]},
    {"title": "Interview", "truth": ["Tom", "Hana"], "lines": [
        (0, "Welcome, Hana. I'm Tom, the hiring manager for this role."),
        (1, "Thanks Tom, nice to meet you."),
        (0, "Could you walk me through a project you're proud of?"),
        (1, "Sure. Last year I led the migration of our billing system to event sourcing."),
        (0, "What was the hardest part?"),
        (1, "Getting the old and new systems to agree during the cut-over."),
    ]},
    {"title": "Retrospective", "truth": ["Grace", None, "Ravi"], "lines": [
        (0, "Let's start the retro. What went well this sprint?"),
        (1, "The new deployment pipeline saved us hours."),
        (2, "Agreed. Grace, you set that up, right?"),
        (0, "I did, with a lot of help. What should we improve?"),
        (1, "Our estimates. We underestimated the search work again."),
        (0, "Good point. Ravi, can you lead the estimation session next week?"),
        (2, "Yes, I'll book it."),
    ]},
]


def transcript(meeting: dict) -> dict:
    """The meeting as a transcript without names (what the identification pass gets)."""
    segments = [{"start": i * 12.0, "end": i * 12.0 + 10, "speaker": f"SPEAKER_{spk:02d}", "text": text}
                for i, (spk, text) in enumerate(meeting["lines"])]
    return {"id": "synthetic", "title": meeting["title"], "segments": segments, "speaker_names": {},
            "speakers": sorted({s["speaker"] for s in segments})}


def score(meeting: dict, labels: dict) -> dict:
    """correct: right first name, or no name for a speaker nobody names; wrong: any other name."""
    out = {"correct": 0, "wrong": 0, "missed": 0}
    for i, name in enumerate(meeting["truth"]):
        got = ((labels.get(f"SPEAKER_{i:02d}") or {}).get("name") or "").split(" ")[0].lower()
        if name is None:
            out["correct" if not got else "wrong"] += 1
        elif not got:
            out["missed"] += 1
        else:
            out["correct" if got == name.lower() else "wrong"] += 1
    return out
