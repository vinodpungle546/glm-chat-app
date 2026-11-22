#!/usr/bin/env python3
"""Confidential-content detection used by Auto mode."""

import pytest

from sensitivity import detect


@pytest.mark.parametrize("text, expected", [
    ("This is CONFIDENTIAL", "keywords"),
    ("Q3 salary bands", "keywords"),
    ("mail me at priya.k@example.co.in", "email address"),
    ("call +91 98765 43210 tomorrow", "phone number"),
    ("PAN ABCDE1234F", "PAN number"),
    ("aadhaar 1234 5678 9012", "Aadhaar number"),
    ("ssn 123-45-6789", "US SSN"),
    ("key sk-abcdefghijklmnopqrstuv", "API key or token"),
    ("card 4111 1111 1111 1111", "payment card number"),
    ("IBAN DE89 3704 0044 0532 0130 00", "IBAN"),
])
def test_detects(text, expected):
    reasons = detect(text)
    assert any(r.startswith(expected) for r in reasons), reasons


@pytest.mark.parametrize("text", [
    "What is the capital of France?",
    "Write a python function to sort a list",
    "Revenue grew 12% in 2025",
    "card-like but invalid 1234 5678 9012 3456",
])
def test_ordinary_text_not_flagged(text):
    assert detect(text) == []


def test_transcript_attachment_is_sensitive():
    reasons = detect("summarise", [{"kind": "transcript", "name": "Transcript: Board"}])
    assert reasons == ["meeting transcript attached (Transcript: Board)"]


def test_attachment_text_scanned():
    assert detect("summarise this", [{"name": "a.txt", "text": "internal only memo"}])


def test_project_confidential_terms():
    reasons = detect("status of Project Falcon?", extra_terms=["Project Falcon", " "])
    assert reasons == ["confidential term for this project: “Project Falcon”"]
    assert detect("falconry tips", extra_terms=["Falcon"]) == []
