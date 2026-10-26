from datetime import date, time

import pytest

from app.brain.entities import (
    extract_datetime,
    extract_name,
    has_dob_without_year,
    match_provider,
    match_service,
    parse_choice,
    parse_dob,
    parse_phone,
)
from app.brain.text import normalize, yes_no

TODAY = date(2026, 10, 26)  # Monday


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Twenty five", "25"),
        ("March fifth at three thirty PM.", "march 5th at 3:30 pm"),
        ("the second one", "the 2nd one"),
        ("that one", "that one"),
        ("at one pm", "at 1 pm"),
        ("half past two", "2:30"),
        ("quarter to 3", "2:45"),
        ("two thousand five", "2005"),
        ("10:30 a.m.", "10:30 am"),
        ("José, please!", "jose please"),
        ("before noon", "before 12 pm"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("yes please", "yes"),
        ("no thanks", "no"),
        ("nope", "no"),
        ("sure thing", "yes"),
        ("not really", "no"),
        ("hmm", None),
        ("", None),
    ],
)
def test_yes_no(raw, expected):
    assert yes_no(normalize(raw)) == expected


def dates(text, **kw):
    return extract_datetime(normalize(text), TODAY, **kw).dates


def times(text, **kw):
    return extract_datetime(normalize(text), TODAY, **kw).times


@pytest.mark.parametrize(
    "text,expected",
    [
        ("tomorrow", date(2026, 10, 27)),
        ("day after tomorrow", date(2026, 10, 28)),
        ("tuesday", date(2026, 10, 27)),
        ("this friday", date(2026, 10, 30)),
        ("monday", date(2026, 11, 2)),  # today is Monday: a bare weekday means the NEXT one
        ("next tuesday", date(2026, 11, 3)),  # "next X" = following calendar week
        ("november 5th", date(2026, 11, 5)),
        ("5 november", date(2026, 11, 5)),
        ("2026-12-01", date(2026, 12, 1)),
        ("in two weeks", date(2026, 11, 9)),
        ("january 3rd", date(2027, 1, 3)),  # already passed this year -> next year
    ],
)
def test_date_parsing(text, expected):
    assert dates(text)[-1].value == expected


def test_last_mention_wins_and_negation_is_skipped():
    assert dates("Tuesday, no wait, Thursday")[-1].value == date(2026, 10, 29)
    assert [d.value for d in dates("not Tuesday, Wednesday instead")] == [date(2026, 10, 28)]
    assert [d.value for d in dates("Thursday instead of Tuesday")] == [date(2026, 10, 29)]


def test_week_and_earliest_and_bare_day():
    assert dates("next week")[0].kind == "week_of"
    assert dates("earliest available")[0].kind == "earliest"
    assert dates("the 5th", allow_bare_day=True)[0].value == date(2026, 11, 5)
    assert dates("the 5th") == []


@pytest.mark.parametrize(
    "text,exact",
    [
        ("3pm", time(15)),
        ("10:30", time(10, 30)),
        ("at 4", time(16)),
        ("at noon", time(12)),
        ("ten thirty am", time(10, 30)),
        ("half past two", time(14, 30)),
    ],
)
def test_exact_times(text, exact):
    assert times(text)[-1].window.exact == exact


def test_time_windows():
    after = times("after 2")[0].window
    assert (after.start, after.end) == (time(14), time(20))
    before = times("before noon")[0].window
    assert before.end == time(11, 59)
    around = times("around 3")[0].window
    assert around.center == time(15)
    assert times("morning")[0].window.start == time(8)
    assert times("good morning") == []  # a greeting is not a time
    b = times("between 2 and 4")[0].window
    assert (b.start, b.end) == (time(14), time(16))


def test_date_digits_are_not_read_as_a_time():
    ent = extract_datetime(normalize("march 5"), TODAY, allow_bare_time=True)
    assert ent.dates and ent.times == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("march 4th 1990", date(1990, 3, 4)),
        ("I was born on the 4th of March 1990", date(1990, 3, 4)),
        ("04/03/1990", date(1990, 4, 3)),
        ("1985-03-14", date(1985, 3, 14)),
        ("march fifth nineteen eighty four", date(1984, 3, 5)),
        ("march 4th", None),
        ("march 4th 2030", None),  # future
    ],
)
def test_dob(text, expected):
    assert parse_dob(normalize(text), TODAY) == expected


def test_dob_without_year_detected():
    assert has_dob_without_year(normalize("march 4th"))
    assert not has_dob_without_year(normalize("hello"))


@pytest.mark.parametrize(
    "text,expecting,expected",
    [
        ("my name is Maria Gonzalez", False, ("Maria", "Gonzalez")),
        ("Maria Gonzalez", True, ("Maria", "Gonzalez")),
        ("Maria Gonzalez", False, (None, None)),  # a bare name is only accepted when we asked for it
        ("I'm sick", True, (None, None)),
        ("I'm calling to book", False, (None, None)),
        ("this is José Hernández and I was born march 4 1990", False, ("Jose", "Hernandez")),
        ("Liam O'Connor", True, ("Liam", "O'Connor")),
        ("Gonzalez", True, ("Gonzalez", None)),
    ],
)
def test_names(text, expecting, expected):
    assert extract_name(normalize(text), expecting=expecting) == expected


def test_phone():
    assert parse_phone(normalize("555 123 4567")) == "5551234567"
    assert parse_phone(normalize("1-555-123-4567")) == "5551234567"
    assert parse_phone(normalize("march 4 1990")) is None


@pytest.mark.parametrize(
    "text,key",
    [
        ("I need a flu shot", "vaccination"),
        ("blood work please", "lab_work"),
        ("a physical", "annual_physical"),
        ("follow-up on my results", "follow_up"),
        ("I have a cough", "general_consultation"),
        ("I feel sick, not a physical", "general_consultation"),
        ("flu shot instead of blood work", "vaccination"),
        ("annual phisical", "annual_physical"),  # typo tolerated
    ],
)
def test_service_matching(rules, text, key):
    svc = match_service(normalize(text), rules)
    assert svc and svc.key == key


@pytest.mark.parametrize(
    "text,expected",
    [
        ("dr patel", "patel"),
        ("Dr. Patell", "patel"),
        ("any doctor", "any"),
        ("no preference", "any"),
        ("not Patel, Nguyen", "nguyen"),
        ("kim", "kim"),
    ],
)
def test_provider_matching(rules, text, expected):
    assert match_provider(normalize(text), rules, expecting=True) == expected


def test_any_time_is_not_any_provider(rules):
    assert match_provider(normalize("any time works"), rules) is None


@pytest.mark.parametrize(
    "text,n,expected",
    [
        ("the 2nd one", 3, 1),
        ("2", 3, 1),
        ("last one", 3, 2),
        ("3 pm", 3, None),
        ("option 1", 3, 0),
        ("the 5th", 3, None),
    ],
)
def test_choice(text, n, expected):
    assert parse_choice(normalize(text), n) == expected


def test_no_comma_before_a_value_is_a_correction_not_a_negation():
    # "Thursday, no, Friday" and "No, 3 pm" must take the value after the "no"
    assert dates("Thursday at 3, no, Friday morning")[-1].value == date(2026, 10, 30)
    assert times("no, 3 pm")[-1].window.exact == time(15)
    assert times("no 3 pm")[-1].window.exact == time(15)  # a leading "no" is a reply particle
    # while a real negation still removes the value
    assert [d.value for d in dates("not tuesday wednesday")] == [date(2026, 10, 28)]
