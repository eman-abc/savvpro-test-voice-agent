"""Urdu and Roman Urdu: understanding, formatting, and completeness of the Urdu replies."""

import ast
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from app.brain import dialog as dialog_module
from app.brain import intents, messages_ur, urdu
from app.brain.text import normalize
from tests.conftest import Call

LATIN_WORD = re.compile(r"[A-Za-z]{3,}")


def ur_call(svc, rules):
    c = Call(svc, rules)
    c.state.lang = "ur"
    c.last = c.brain.start(c.state)
    return c


def canon(text: str) -> str:
    return normalize(urdu.to_canonical(text))


# ----------------------------------------------------------------------- understanding ---
@pytest.mark.parametrize(
    "text,expected",
    [
        ("مجھے کل صبح 10 بجے اپائنٹمنٹ چاہیے", ["tomorrow", "10 am", "book appointment"]),
        ("میری اپائنٹمنٹ منسوخ کریں", ["cancel", "appointment"]),
        ("منگل شام 5 بجے", ["tuesday", "5 pm"]),
        ("پرسوں دوپہر 2 بجے", ["day after tomorrow", "2 pm"]),
        ("ساڑھے تین بجے", ["3:30"]),
        ("پونے چار بجے", ["3:45"]),
        ("ڈھائی بجے", ["2:30"]),
        ("میری تاریخ پیدائش پندرہ مارچ انیس سو پچاسی ہے", ["15 march 1985"]),
        ("دو ہزار پانچ", ["2005"]),
        ("۱۴ مارچ ۱۹۸۵", ["14 march 1985"]),  # Urdu-Indic digits
        ("جی ہاں", ["yes"]),
        ("نہیں شکریہ", ["no"]),
        ("آپ کے اوقات کیا ہیں", ["what", "hours"]),
        ("سینے میں درد ہے", ["chest pain"]),
        ("کسی انسان سے بات کرنی ہے", ["talk to a real person"]),
        ("فلو کا ٹیکہ چاہیے", ["flu shot"]),
        ("اگلے ہفتے منگل", ["next week", "tuesday"]),
        ("جلد سے جلد", ["earliest available"]),
        ("میں آ نہیں سکتا", ["can't make it"]),
    ],
)
def test_urdu_script_is_understood(text, expected):
    got = canon(text)
    for piece in expected:
        assert piece in got, f"{text!r} -> {got!r} lacks {piece!r}"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("mujhe kal subah 10 baje appointment chahiye", ["tomorrow", "10 am", "book appointment"]),
        ("meri appointment cancel karni hai", ["cancel", "appointment"]),
        ("mera naam Wei Zhang hai", ["my name is", "wei zhang"]),
        ("haan theek hai", ["yes"]),
        ("nahi shukriya", ["no", "thank you"]),
    ],
)
def test_roman_urdu_is_understood(text, expected):
    assert urdu.is_urdu(text)
    got = canon(text)
    for piece in expected:
        assert piece in got


def test_plain_english_is_left_alone():
    for text in ["I do want an appointment", "book a flu shot tomorrow at ten", "yes please"]:
        assert not urdu.is_urdu(text)


def test_urdu_number_words_are_in_the_right_places():
    known = {
        "پندرہ": 15,
        "اکیس": 21,
        "تیس": 30,
        "چالیس": 40,
        "انچاس": 49,
        "انسٹھ": 59,
        "اڑسٹھ": 68,
        "اناسی": 79,
        "اسی": 80,
        "پچاسی": 85,
        "نوے": 90,
        "بانوے": 92,
        "ننانوے": 99,
        "تراسی": 83,
        "تینتیس": 33,
        "اڑتالیس": 48,
    }
    for word, value in known.items():
        assert urdu.NUM_WORDS[word] == value, word


def test_urdu_names_are_transliterated_for_phonetic_matching():
    assert urdu.transliterate("احمد") == "ahmd"
    from app.textutil import phonetic_key

    assert phonetic_key(urdu.transliterate("احمد")) == phonetic_key("Ahmed")
    assert phonetic_key(urdu.transliterate("خان")) == phonetic_key("Khan")
    assert phonetic_key(urdu.transliterate("اوکونر")) == phonetic_key("O'Connor")


# ------------------------------------------------------------------------- formatting ---
def test_urdu_dates_times_and_lists():
    d, today = date(2026, 11, 3), date(2026, 10, 26)
    assert urdu.ur_date(d, today) == "منگل، 3 نومبر"
    assert urdu.ur_on_date(d, today) == "منگل، 3 نومبر کو"
    assert urdu.ur_on_date(date(2026, 10, 27), today) == "کل"
    assert urdu.ur_on_date(today, today) == "آج"
    assert urdu.ur_time(datetime(2026, 11, 3, 10, 0)) == "صبح 10 بجے"
    assert urdu.ur_time(datetime(2026, 11, 3, 14, 30)) == "دوپہر 2:30 بجے"
    assert urdu.ur_time(datetime(2026, 11, 3, 17, 15)) == "شام 5:15 بجے"
    assert urdu.ur_join(["الف", "ب", "ج"]) == "الف، ب یا ج"
    assert urdu.ur_weekdays([0, 2, 1]) == "پیر، منگل اور بدھ"
    assert urdu.ur_dob(date(1985, 3, 14)) == "14 مارچ 1985"


# ----------------------------------------------------------- catalogue completeness ---
def _templates_used_by_the_brain() -> set[str]:
    tree = ast.parse(Path(dialog_module.__file__).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value.strip())
    found.add(dialog_module.MENU)
    found |= set(dialog_module.HINTS.values()) | set(dialog_module.PAIR_TEXT.values()) | set(intents.LABELS.values())
    return {t for t in found if t}


def test_every_template_the_brain_can_say_has_an_urdu_translation():
    missing = sorted(t for t in _templates_used_by_the_brain() if t not in messages_ur.UR)
    assert missing == [], f"untranslated: {missing}"


def test_translations_keep_the_same_placeholders_and_have_urdu_script():
    for en, ur in messages_ur.UR.items():
        # a translation may omit a placeholder (e.g. a Latin name) but must never invent one
        assert set(re.findall(r"\{\d+\}", ur)) <= set(re.findall(r"\{\d+\}", en)), en
        if re.sub(r"\{\d+\}|\s", "", ur):
            assert urdu.has_urdu_script(ur), f"no Urdu script in translation of {en!r}"


def test_every_rule_name_and_faq_has_urdu_text(rules):
    assert rules.clinic.name_ur
    assert all(p.name_ur and p.title_ur for p in rules.providers)
    assert all(s.name_ur for s in rules.services)
    assert all(f.answer_ur and f.question_ur for f in rules.faq)


# --------------------------------------------------------------------- whole dialogues ---
def says(call, *lines):
    replies = []
    for line in lines:
        replies.append(call.say(line))
        if call.last.ended:
            break
    return replies


def test_urdu_booking_end_to_end(svc, rules):
    c = ur_call(svc, rules)
    says(
        c,
        "مجھے فلو کا ٹیکہ چاہیے",
        "میرا نام احمد خان ہے",
        "میری تاریخ پیدائش بارہ جون انیس سو تراسی ہے",
        "کوئی بھی ڈاکٹر",
        "کل صبح 10 بجے",
    )
    assert c.step == "CONFIRM" and "ویکسینیشن" in c.last.reply and "صبح 10 بجے" in c.last.reply
    c.say("نہیں، شام 3 بجے")  # a correction in Urdu keeps us at the confirmation, with the new time
    assert c.step == "CONFIRM" and "دوپہر 3 بجے" in c.last.reply
    c.say("جی ہاں")
    assert c.step == "MORE" and "بک ہو گئی" in c.last.reply and c.state.outcome() == "booked"


def test_urdu_replies_never_leak_english(svc, rules):
    patient_names = {"Ahmed", "Ahmd", "Khan", "Liam", "Connor", "Maria", "Wei", "Zhang", "Priya", "Sharma"}
    scripts = [
        [
            "مجھے فلو کا ٹیکہ چاہیے",
            "میرا نام احمد خان ہے",
            "میری تاریخ پیدائش بارہ جون انیس سو تراسی ہے",
            "کوئی بھی ڈاکٹر",
            "جلد سے جلد",
            "پہلا والا",
            "جی ہاں",
            "نہیں شکریہ",
        ],
        ["میری اپائنٹمنٹ منسوخ کریں", "لیام اوکونر، تیس جنوری انیس سو اڑسٹھ", "جی ہاں"],
        ["آپ کے اوقات کیا ہیں", "پارکنگ کہاں ہے", "کیا ساتھ لانا ہے", "نہیں"],
        ["مجھے سینے میں درد ہے"],
        ["مجھے کسی انسان سے بات کرنی ہے"],
        ["میں اپنی اپائنٹمنٹ پر آ نہیں سکتا", "منسوخ"],
        [
            "مجھے اپائنٹمنٹ چاہیے، میں نیا مریض ہوں",
            "علی رضا",
            "پانچ مئی انیس سو بانوے",
            "5551234567",
            "بخار ہے",
            "کوئی بھی",
            "جمعہ صبح 9 بجے",
            "جی ہاں",
        ],
        ["asdfgh", "qwerty", "zxcvb"],
    ]
    for lines in scripts:
        c = ur_call(svc, rules)
        for reply in [c.last.reply, *says(c, *lines)]:
            leaked = [w for w in LATIN_WORD.findall(reply) if w not in patient_names]
            assert not leaked, f"English leaked into Urdu reply {reply!r}: {leaked}"


def test_language_switches_to_urdu_when_the_caller_speaks_it(call):
    assert call.state.lang == "en"
    call.say("مجھے اپائنٹمنٹ چاہیے")
    assert call.state.lang == "ur" and urdu.has_urdu_script(call.last.reply)
    assert any("Urdu understood" in s["label"] for s in call.trace["steps"])


def test_roman_urdu_is_answered_in_urdu_script(call):
    call.say("mujhe kal subah 10 baje appointment chahiye")
    assert call.state.lang == "ur" and call.step == "IDENTIFY" and urdu.has_urdu_script(call.last.reply)


def test_english_stays_english_for_english_speakers(call):
    call.say("I need a flu shot")
    assert call.state.lang == "en" and not urdu.has_urdu_script(call.last.reply)


def test_urdu_emergency_gives_the_local_number(svc, rules):
    c = ur_call(svc, rules)
    c.say("مجھے سینے میں درد ہے")
    assert c.last.ended and "1122" in c.last.reply and "911" not in c.last.reply


def test_urdu_faq_answers_come_from_the_rules_file(svc, rules):
    c = ur_call(svc, rules)
    assert "مفت پارکنگ" in c.say("پارکنگ کہاں ہے")
    c2 = ur_call(svc, rules)
    assert "صبح 8 بجے" in c2.say("آپ کے اوقات کیا ہیں")


def test_urdu_identity_failure_never_reveals_which_part(svc, rules):
    c = ur_call(svc, rules)
    c.say("اپائنٹمنٹ بک کرنی ہے")
    reply = c.say("جان سمتھ، پہلی جنوری انیس سو اسی")
    assert "ریکارڈ سے نہیں ملیں" in reply


def test_english_correction_with_a_leading_no(call):
    call.say("book a flu shot, I'm Wei Zhang born may 17 1995")
    call.say("any")
    call.say("next tuesday at 10")
    call.say("no, 3 pm")
    assert call.step == "CONFIRM" and "3 PM" in call.last.reply
