"""Urdu support: formatting for replies, and understanding of Urdu script and Roman Urdu.

Understanding works by translating what the caller said into the small canonical English vocabulary the
brain's patterns already use ("کل صبح 10 بجے" -> "tomorrow 10 am"). That keeps ONE set of rules for every
language: intents, slot filling, corrections and policy behave identically in English and Urdu.
Names are not translated; unknown Urdu words are transliterated so the phonetic matcher can compare them
with the (Latin-script) patient records.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# ============================================================================ formatting ===
UR_WEEKDAYS = ["پیر", "منگل", "بدھ", "جمعرات", "جمعہ", "ہفتہ", "اتوار"]
UR_MONTHS = ["جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون", "جولائی", "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر"]


def ur_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    period = "صبح" if h < 12 else "دوپہر" if h < 16 else "شام" if h < 19 else "رات"
    return f"{period} {h % 12 or 12}{f':{m:02d}' if m else ''} بجے"


def ur_date(d: date, today: date | None = None) -> str:
    if today:
        if d == today:
            return "آج"
        if (d - today).days == 1:
            return "کل"
    return f"{UR_WEEKDAYS[d.weekday()]}، {d.day} {UR_MONTHS[d.month - 1]}"


def ur_on_date(d: date, today: date | None = None) -> str:
    pretty = ur_date(d, today)
    return pretty if pretty in ("آج", "کل") else f"{pretty} کو"


def ur_join(items: list[str], conj: str = "یا") -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{'، '.join(items[:-1])} {conj} {items[-1]}"


def ur_weekdays(days: list[int]) -> str:
    return ur_join([UR_WEEKDAYS[d] for d in sorted(days)], "اور")


def ur_dob(d: date) -> str:
    return f"{d.day} {UR_MONTHS[d.month - 1]} {d.year}"


# ============================================================================ detection ===
_URDU_LETTER = re.compile(r"[\u0600-\u06FF\u0750-\u077F]")
ROMAN_MARKERS = {
    "mujhe",
    "mera",
    "meri",
    "mere",
    "hai",
    "hain",
    "hoon",
    "chahiye",
    "chahie",
    "karna",
    "karni",
    "karein",
    "kijiye",
    "kijiyega",
    "kal",
    "aaj",
    "parson",
    "haan",
    "nahi",
    "nahin",
    "shukriya",
    "baje",
    "subah",
    "dopahar",
    "shaam",
    "aap",
    "kya",
    "kab",
    "kahan",
    "lena",
    "lene",
    "lijiye",
    "theek",
    "bilkul",
    "zaroor",
    "naam",
    "pehle",
    "baad",
    "dobara",
    "badal",
    "badalna",
    "mein",
    "main",
    "ko",
    "se",
    "ka",
    "ki",
    "ke",
    "wala",
    "wali",
    "abhi",
    "kitne",
    "kitna",
    "bukhar",
    "khansi",
    "dard",
    "tabiyat",
    "khuda",
    "hafiz",
    "salam",
    "assalam",
}
_STRONG_ROMAN = ROMAN_MARKERS - {"ko", "se", "ka", "ki", "ke", "mein", "main", "hai", "baad", "pehle"}


def has_urdu_script(text: str) -> bool:
    return bool(_URDU_LETTER.search(text))


def looks_roman_urdu(text: str) -> bool:
    toks = re.findall(r"[a-z]+", text.lower())
    return sum(t in ROMAN_MARKERS for t in toks) >= 2 and any(t in _STRONG_ROMAN for t in toks)


def is_urdu(text: str) -> bool:
    return has_urdu_script(text) or looks_roman_urdu(text)


# ============================================================================== numbers ===
_UR_UNITS = [
    "صفر",
    "ایک",
    "دو",
    "تین",
    "چار",
    "پانچ",
    "چھ",
    "سات",
    "آٹھ",
    "نو",
    "دس",
    "گیارہ",
    "بارہ",
    "تیرہ",
    "چودہ",
    "پندرہ",
    "سولہ",
    "سترہ",
    "اٹھارہ",
    "انیس",
    "بیس",
    "اکیس",
    "بائیس",
    "تئیس",
    "چوبیس",
    "پچیس",
    "چھبیس",
    "ستائیس",
    "اٹھائیس",
    "انتیس",
    "تیس",
    "اکتیس",
    "بتیس",
    "تینتیس",
    "چونتیس",
    "پینتیس",
    "چھتیس",
    "سینتیس",
    "اڑتیس",
    "انتالیس",
    "چالیس",
    "اکتالیس",
    "بیالیس",
    "تینتالیس",
    "چوالیس",
    "پینتالیس",
    "چھیالیس",
    "سینتالیس",
    "اڑتالیس",
    "انچاس",
    "پچاس",
    "اکاون",
    "باون",
    "تریپن",
    "چون",
    "پچپن",
    "چھپن",
    "ستاون",
    "اٹھاون",
    "انسٹھ",
    "ساٹھ",
    "اکسٹھ",
    "باسٹھ",
    "تریسٹھ",
    "چونسٹھ",
    "پینسٹھ",
    "چھیاسٹھ",
    "سڑسٹھ",
    "اڑسٹھ",
    "انہتر",
    "ستر",
    "اکہتر",
    "بہتر",
    "تہتر",
    "چوہتر",
    "پچھتر",
    "چھہتر",
    "ستتر",
    "اٹھتر",
    "اناسی",
    "اسی",
    "اکیاسی",
    "بیاسی",
    "تراسی",
    "چوراسی",
    "پچاسی",
    "چھیاسی",
    "ستاسی",
    "اٹھاسی",
    "نواسی",
    "نوے",
    "اکانوے",
    "بانوے",
    "ترانوے",
    "چورانوے",
    "پچانوے",
    "چھیانوے",
    "ستانوے",
    "اٹھانوے",
    "ننانوے",
]
NUM_WORDS: dict[str, int] = {w: i for i, w in enumerate(_UR_UNITS)}
NUM_WORDS.update({"چھے": 6, "ایک": 1})
_ROMAN_NUMS = [
    "ek",
    "do",
    "teen",
    "char",
    "paanch",
    "panch",
    "chhe",
    "chhay",
    "saat",
    "aath",
    "nau",
    "das",
    "gyarah",
    "barah",
    "terah",
    "chaudah",
    "pandrah",
    "solah",
    "satrah",
    "atharah",
    "unnees",
    "bees",
]
_ROMAN_VALUES = [1, 2, 3, 4, 5, 5, 6, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
ROMAN_NUM_WORDS = dict(zip(_ROMAN_NUMS, _ROMAN_VALUES, strict=True))
ROMAN_NUM_WORDS.update({"tees": 30, "chalis": 40, "pachas": 50})
HUNDRED, THOUSAND = {"سو", "sau"}, {"ہزار", "hazar", "hazaar"}
FRACTIONS = {  # spoken clock idioms -> h:mm
    "ساڑھے": ":30",
    "sarhe": ":30",
    "sade": ":30",
    "saray": ":30",
    "sadhe": ":30",
    "saarhe": ":30",
    "سوا": ":15",
    "sawa": ":15",
    "پونے": ":45",
    "paune": ":45",
    "pone": ":45",
}
HALF_WORDS = {"ڈیڑھ": "1:30", "derh": "1:30", "dedh": "1:30", "ڈھائی": "2:30", "dhai": "2:30"}
_AM, _PM = {"صبح", "subah"}, {"دوپہر", "سہ", "شام", "رات", "dopahar", "shaam", "sham", "raat"}


def _clean(text: str) -> str:
    t = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    t = re.sub(r"[\u064B-\u065F\u0670\u0640\u200c\u200d]", "", t)
    for a, b in (("ي", "ی"), ("ك", "ک"), ("ۃ", "ہ"), ("ە", "ہ"), ("ئ", "ئ"), ("ے", "ے"), ("ى", "ی"), ("ہ‌", "ہ")):
        t = t.replace(a, b)
    return re.sub(r"[۔،؟!?,;:.\"()]+", " ", t.lower())


def _numbers_and_clock(tokens: list[str], roman: bool) -> list[str]:
    nums = {**NUM_WORDS, **(ROMAN_NUM_WORDS if roman else {})}
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in HALF_WORDS and (i + 1 < len(tokens) and tokens[i + 1] in {"بجے", "baje"} or True):
            out.append(HALF_WORDS[t])
            i += 1
            continue
        if t in nums or t.isdigit():
            val = nums[t] if t in nums else int(t)
            j = i + 1
            if j < len(tokens) and tokens[j] in HUNDRED:  # انیس سو پچاسی -> 1985
                val, j = val * 100, j + 1
                if j < len(tokens) and (tokens[j] in nums or tokens[j].isdigit()):
                    val += nums.get(tokens[j], int(tokens[j]) if tokens[j].isdigit() else 0)
                    j += 1
            elif j < len(tokens) and tokens[j] in THOUSAND:  # دو ہزار پانچ -> 2005
                val, j = val * 1000, j + 1
                if j < len(tokens) and (tokens[j] in nums or tokens[j].isdigit()):
                    val += nums.get(tokens[j], int(tokens[j]) if tokens[j].isdigit() else 0)
                    j += 1
            out.append(str(val))
            i = j
            continue
        out.append(t)
        i += 1
    # ساڑھے 3 -> 3:30, پونے 3 -> 2:45
    res: list[str] = []
    i = 0
    while i < len(out):
        t = out[i]
        if t in FRACTIONS and i + 1 < len(out) and out[i + 1].isdigit():
            n = int(out[i + 1])
            if FRACTIONS[t] == ":45":
                n = (n - 2) % 12 + 1
            res.append(f"{n}{FRACTIONS[t]}")
            i += 2
            continue
        res.append(t)
        i += 1
    return res


def _periods(tokens: list[str]) -> list[str]:
    """صبح 10 بجے -> "10 am"; شام 5 بجے -> "5 pm"; 3 بجے -> "3 o'clock"."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "سہ" and i + 1 < len(tokens) and tokens[i + 1] == "پہر":
            tokens = tokens[: i + 1] + tokens[i + 2 :]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if (t in _AM or t in _PM) and re.fullmatch(r"\d{1,2}(:\d{2})?", nxt):
            out.append(f"{nxt} {'am' if t in _AM else 'pm'}")
            i += 2
            if i < len(tokens) and tokens[i] in {"بجے", "baje"}:
                i += 1
            continue
        if re.fullmatch(r"\d{1,2}(:\d{2})?", t) and nxt in {"بجے", "baje"}:
            out.append(f"{t} o'clock")
            i += 2
            continue
        out.append(t)
        i += 1
    return out


# ============================================================================= lexicon ===
# multi-word phrases first (longest match wins), then single words. Values are canonical English.
PHRASES: list[tuple[str, str]] = [
    ("سینے میں درد", "chest pain"),
    ("سینے کا درد", "chest pain"),
    ("سانس نہیں آ", "cannot breathe"),
    ("سانس لینے میں", "trouble breathing"),
    ("دل کا دورہ", "heart attack"),
    ("خون بہہ رہا", "bleeding heavily"),
    ("شدید خون", "severe bleeding"),
    ("شدید الرجی", "allergic reaction"),
    ("seene mein dard", "chest pain"),
    ("saans nahi", "cannot breathe"),
    ("dil ka daura", "heart attack"),
    ("اپائنٹمنٹ بک", "book appointment"),
    ("اپوائنٹمنٹ بک", "book appointment"),
    ("وقت لینا", "book appointment"),
    ("وقت لینی", "book appointment"),
    ("وقت چاہیے", "book appointment"),
    ("وقت چاہئے", "book appointment"),
    ("وقت بک", "book appointment"),
    ("waqt lena", "book appointment"),
    ("waqt chahiye", "book appointment"),
    ("فلو کا ٹیکہ", "flu shot"),
    ("فلو کا ٹیکا", "flu shot"),
    ("فلو شاٹ", "flu shot"),
    ("flu ka teeka", "flu shot"),
    ("خون کا ٹیسٹ", "blood test"),
    ("خون کے ٹیسٹ", "blood test"),
    ("بلڈ ٹیسٹ", "blood test"),
    ("khoon ka test", "blood test"),
    ("ٹیسٹ کے نتائج", "test results"),
    ("رپورٹ دیکھ", "test results"),
    ("چیک اپ", "check up"),
    ("سالانہ معائنہ", "annual physical"),
    ("جسمانی معائنہ", "physical"),
    ("گلا خراب", "sore throat"),
    ("gala kharab", "sore throat"),
    ("طبیعت خراب", "not feeling well"),
    ("tabiyat kharab", "not feeling well"),
    ("نیا مریض", "new patient"),
    ("نئی مریض", "new patient"),
    ("naya mareez", "new patient"),
    ("پہلی بار", "first time"),
    ("pehli baar", "first time"),
    ("تاریخ پیدائش", "date of birth"),
    ("tareekh e paidaish", "date of birth"),
    ("میرا نام", "my name is"),
    ("mera naam", "my name is"),
    ("میری تاریخ پیدائش", "my date of birth"),
    ("دوبارہ طے", "reschedule"),
    ("dobara tay", "reschedule"),
    ("آگے پیچھے", "reschedule"),
    ("وقت بدل", "reschedule appointment"),
    ("وقت تبدیل", "reschedule appointment"),
    ("waqt badal", "reschedule appointment"),
    ("آ نہیں سکتا", "can't make it"),
    ("آ نہیں سکتی", "can't make it"),
    ("نہیں آ پاؤں", "can't make it"),
    ("نہیں آ سکوں", "can't make it"),
    ("aa nahi sakta", "can't make it"),
    ("aa nahi sakti", "can't make it"),
    ("نہیں آ سکتا", "can't make it"),
    ("نہیں آ سکتی", "can't make it"),
    ("نہیں آسکتا", "can't make it"),
    ("نہیں آسکتی", "can't make it"),
    ("nahi aa sakta", "can't make it"),
    ("nahi aa sakti", "can't make it"),
    ("کسی بھی ڈاکٹر", "any doctor"),
    ("کوئی بھی ڈاکٹر", "any doctor"),
    ("koi bhi doctor", "any doctor"),
    ("ویٹنگ لسٹ", "waitlist"),
    ("مجھے اطلاع دیں", "notify me"),
    ("مجھے بتا دیں", "notify me"),
    ("waiting list", "waitlist"),
    ("جلد سے جلد", "earliest available"),
    ("سب سے پہلے دستیاب", "earliest available"),
    ("pehli dastiyab", "earliest available"),
    ("jald se jald", "earliest available"),
    ("کوئی ترجیح نہیں", "no preference"),
    ("کوئی بھی وقت", "any time"),
    ("kisi bhi waqt", "any time"),
    ("اگلے ہفتے", "next week"),
    ("اگلا ہفتہ", "next week"),
    ("اگلی ہفتے", "next week"),
    ("agle hafte", "next week"),
    ("اس ہفتے", "this week"),
    ("is hafte", "this week"),
    ("پرسوں", "day after tomorrow"),
    ("parson", "day after tomorrow"),
    ("جی ہاں", "yes"),
    ("جی نہیں", "no"),
    ("ji haan", "yes"),
    ("ji nahi", "no"),
    ("ٹھیک ہے", "yes"),
    ("theek hai", "yes"),
    ("رہنے دیں", "never mind"),
    ("رہنے دو", "never mind"),
    ("rehne dein", "never mind"),
    ("نئے سرے سے", "start over"),
    ("شروع سے", "start over"),
    ("دوبارہ بولیں", "say that again"),
    ("دہرائیں", "repeat"),
    ("phir se bolein", "say that again"),
    ("اور کچھ نہیں", "nothing else"),
    ("بس یہی", "that's all"),
    ("بس شکریہ", "that's all"),
    ("کسی انسان سے", "talk to a real person"),
    ("کسی شخص سے", "talk to a real person"),
    ("ریسپشنسٹ سے", "talk to a receptionist"),
    ("آپریٹر سے", "talk to an operator"),
    ("kisi insaan se", "talk to a real person"),
    ("receptionist se", "talk to a receptionist"),
    ("کتنے بجے", "what time"),
    ("کب کھل", "when open"),
    ("کب بند", "when closing"),
    ("kab khulte", "when open"),
    ("kitne baje", "what time"),
    ("اوقات کار", "opening hours"),
    ("اوقات", "hours"),
    ("کھلنے کا وقت", "opening hours"),
    ("ساتھ لانا", "bring"),
    ("ساتھ لے کر", "bring"),
    ("sath lana", "bring"),
    ("کتنی دیر پہلے", "how early"),
    ("فون نمبر", "phone number"),
    ("phone number", "phone number"),
    ("نئے مریض", "new patients"),
    ("منسوخی کی پالیسی", "cancellation policy"),
    ("کینسلیشن پالیسی", "cancellation policy"),
]
WORDS: dict[str, str] = {
    "comma": ",",
    # nouns / intents
    "اپائنٹمنٹ": "appointment",
    "اپوائنٹمنٹ": "appointment",
    "اپائنٹ": "appointment",
    "اپائنٹمنٹس": "appointments",
    "ملاقات": "appointment",
    "وقت": "time",
    "waqt": "time",
    "ڈاکٹر": "doctor",
    "ڈاکٹرصاحب": "doctor",
    "نرس": "nurse",
    "بک": "book",
    "بکنگ": "booking",
    "منسوخ": "cancel",
    "کینسل": "cancel",
    "منسوخی": "cancel",
    "ختم": "cancel",
    "تبدیل": "change",
    "بدل": "change",
    "بدلنا": "change",
    "بدلنی": "change",
    "ریشیڈول": "reschedule",
    "ری": "",
    "شیڈول": "reschedule",
    "badal": "change",
    "badalna": "change",
    "tabdeel": "change",
    "ٹیکہ": "vaccine",
    "ٹیکا": "vaccine",
    "ویکسین": "vaccine",
    "teeka": "vaccine",
    "لیب": "lab",
    "معائنہ": "check up",
    "فالو": "follow",
    "اپ": "up",
    "بخار": "fever",
    "bukhar": "fever",
    "کھانسی": "cough",
    "khansi": "cough",
    "زکام": "cold",
    "نزلہ": "cold",
    "درد": "pain",
    "dard": "pain",
    "بیمار": "sick",
    "bimar": "sick",
    "ایمرجنسی": "emergency",
    "ہنگامی": "emergency",
    "بیہوش": "unconscious",
    "بے ہوش": "unconscious",
    "فالج": "stroke",
    "خودکشی": "suicide",
    "اوورڈوز": "overdose",
    "زہر": "overdose",
    # people, politeness, flow
    "شکریہ": "thank you",
    "شکریا": "thank you",
    "shukriya": "thank you",
    "خدا": "",
    "حافظ": "goodbye",
    "الوداع": "goodbye",
    "hafiz": "goodbye",
    "ہیلو": "hello",
    "سلام": "hello",
    "السلام": "hello",
    "آداب": "hello",
    "salam": "hello",
    "assalam": "hello",
    "مدد": "help",
    "madad": "help",
    "دہرائیں": "repeat",
    "ضرور": "yes",
    "بالکل": "yes",
    "bilkul": "yes",
    "zaroor": "yes",
    "درست": "yes",
    "صحیح": "yes",
    "sahi": "yes",
    "اوکے": "ok",
    "ہاں": "yes",
    "haan": "yes",
    "han": "yes",
    "haa": "yes",
    "جی": "yes",
    "ji": "yes",
    "jee": "yes",
    "نہیں": "no",
    "nahi": "no",
    "nahin": "no",
    "nai": "no",
    "نہ": "no",
    "مت": "no",
    "اور": "and",
    "aur": "and",
    "پھر": "then",
    "phir": "then",
    # dates and times
    "آج": "today",
    "aaj": "today",
    "کل": "tomorrow",
    "kal": "tomorrow",
    "اگلے": "next",
    "اگلا": "next",
    "اگلی": "next",
    "agle": "next",
    "پیر": "monday",
    "peer": "monday",
    "pir": "monday",
    "منگل": "tuesday",
    "mangal": "tuesday",
    "بدھ": "wednesday",
    "budh": "wednesday",
    "جمعرات": "thursday",
    "jumerat": "thursday",
    "jumeraat": "thursday",
    "جمعہ": "friday",
    "جمعے": "friday",
    "juma": "friday",
    "jumma": "friday",
    "ہفتہ": "saturday",
    "ہفتے": "saturday",
    "اتوار": "sunday",
    "itwar": "sunday",
    "itwaar": "sunday",
    "جنوری": "january",
    "فروری": "february",
    "مارچ": "march",
    "اپریل": "april",
    "مئی": "may",
    "جون": "june",
    "جولائی": "july",
    "اگست": "august",
    "ستمبر": "september",
    "اکتوبر": "october",
    "نومبر": "november",
    "دسمبر": "december",
    "janwari": "january",
    "farwari": "february",
    "julai": "july",
    "agast": "august",
    "sitambar": "september",
    "aktubar": "october",
    "navambar": "november",
    "disambar": "december",
    "صبح": "morning",
    "subah": "morning",
    "دوپہر": "afternoon",
    "dopahar": "afternoon",
    "سہ": "",
    "پہر": "afternoon",
    "شام": "evening",
    "shaam": "evening",
    "sham": "evening",
    "رات": "evening",
    "raat": "evening",
    "بجے": "o'clock",
    "baje": "o'clock",
    "پہلے": "first",
    "پہلا": "first",
    "پہلی": "first",
    "دوسرا": "second",
    "دوسری": "second",
    "دوسرے": "second",
    "تیسرا": "third",
    "تیسری": "third",
    "آخری": "last",
    "آخر": "last",
    "نمبر": "number",
    "کوئی": "any",
    "کسی": "any",
    "koi": "any",
    # asking
    "کیا": "",
    "kya": "",
    "کب": "when",
    "kab": "when",
    "کہاں": "where",
    "kahan": "where",
    "کتنا": "how much",
    "کتنی": "how much",
    "کتنے": "how many",
    "پارکنگ": "parking",
    "parking": "parking",
    "پتہ": "address",
    "پتا": "address",
    "لوکیشن": "location",
    "انشورنس": "insurance",
    "فیس": "fee",
    "قیمت": "cost",
    "خرچہ": "cost",
    "پیسے": "cost",
    "خدمات": "services",
    "سروسز": "services",
    "پالیسی": "policy",
    "کھلتے": "open",
    "کھلا": "open",
    "بند": "closing",
    "رجسٹر": "register",
    "نام": "name",
    # booking verbs mark "want"; resolved by sentence rules
    "چاہیے": "__want__",
    "چاہئے": "__want__",
    "چاہتا": "__want__",
    "چاہتی": "__want__",
    "لینی": "__want__",
    "لینا": "__want__",
    "لینے": "__want__",
    "لیں": "__want__",
    "کروانی": "__want__",
    "کروانا": "__want__",
    "بنوانی": "__want__",
    "بنوانا": "__want__",
    "chahiye": "__want__",
    "chahie": "__want__",
    "lena": "__want__",
    "lene": "__want__",
    "lijiye": "__want__",
    "banwana": "__want__",
    "بات": "__talk__",
    "baat": "__talk__",
}
# grammatical glue with no content: dropped (so it is not mistaken for a name)
STOP_UR = set(
    [
        "میری",
        "میرا",
        "میرے",
        "meri",
        "mera",
        "mere",
        "میں",
        "مجھے",
        "مجھ",
        "ہے",
        "ہیں",
        "ہوں",
        "ہو",
        "تھا",
        "تھی",
        "تھے",
        "گا",
        "گی",
        "گے",
        "کو",
        "کا",
        "کی",
        "کے",
        "سے",
        "پر",
        "نے",
        "یہ",
        "وہ",
        "اس",
        "ان",
        "آپ",
        "آپکا",
        "آپکی",
        "ہم",
        "کر",
        "کرنا",
        "کرنی",
        "کریں",
        "کرنے",
        "کروں",
        "دیں",
        "دیجئے",
        "دیجیے",
        "کیجئے",
        "کیجیے",
        "سکتا",
        "سکتی",
        "سکتے",
        "رہا",
        "رہی",
        "رہے",
        "لیے",
        "لئے",
        "بھی",
        "تو",
        "ہی",
        "اب",
        "جب",
        "تک",
        "ساتھ",
        "اندر",
        "والا",
        "والی",
        "والے",
        "mein",
        "main",
        "mujhe",
        "hai",
        "hain",
        "hoon",
        "ko",
        "ka",
        "ki",
        "ke",
        "se",
        "par",
        "ye",
        "woh",
        "is",
        "un",
        "aap",
        "hum",
        "karna",
        "karni",
        "karein",
        "kijiye",
        "kijiyega",
        "dijiye",
        "sakta",
        "sakti",
        "raha",
        "rahi",
        "rahe",
        "liye",
        "bhi",
        "to",
        "hi",
        "ab",
        "tak",
        "wala",
        "wali",
        "wale",
    ]
)
_PHRASE_TOKENS = sorted(((tuple(p.split()), c) for p, c in PHRASES), key=lambda x: -len(x[0]))

# ============================================================================ translit ===
_L = {
    "ا": "a",
    "آ": "aa",
    "ب": "b",
    "پ": "p",
    "ت": "t",
    "ٹ": "t",
    "ث": "s",
    "ج": "j",
    "چ": "ch",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ڈ": "d",
    "ذ": "z",
    "ر": "r",
    "ڑ": "r",
    "ز": "z",
    "ژ": "zh",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "z",
    "ط": "t",
    "ظ": "z",
    "ع": "a",
    "غ": "gh",
    "ف": "f",
    "ق": "q",
    "ک": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "ں": "n",
    "و": "o",
    "ہ": "h",
    "ھ": "h",
    "ء": "",
    "ی": "i",
    "ے": "e",
    "ئ": "i",
    "ؤ": "o",
    "أ": "a",
    "ۓ": "e",
}


def transliterate(word: str) -> str:
    """Rough Urdu -> Latin so a name can be compared phonetically with Latin-script records."""
    out = []
    for i, ch in enumerate(word):
        if ch == "و" and i == 0:
            out.append("w")
        elif ch == "ا" and i > 0 and out and out[-1].endswith("a"):
            continue
        elif ch == "ا" and i > 0:
            out.append("aa")
        else:
            out.append(_L.get(ch, ch))
    return "".join(out)


# ================================================================================ main ===
def _map_tokens(tokens: list[str], names: dict[str, str] | None = None) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        for phrase, canon in _PHRASE_TOKENS:
            n = len(phrase)
            if tuple(tokens[i : i + n]) == phrase:
                out.append(canon)
                i += n
                break
        else:
            t = tokens[i]
            if t in WORDS:
                out.append(WORDS[t])
            elif t in STOP_UR:
                pass
            elif _URDU_LETTER.search(t):
                latin = transliterate(t)
                if names is not None:
                    names[latin] = t  # remember the Urdu spelling: the reply can echo it back
                out.append(latin)
            else:
                out.append(t)
            i += 1
    return out


def to_canonical(text: str, names: dict[str, str] | None = None) -> str:
    """Urdu or Roman Urdu -> canonical English tokens understood by the rest of the brain."""
    roman = looks_roman_urdu(text) and not has_urdu_script(text)
    text = re.sub(r"(نہیں|nahi|nahin|nope)\s*[،,]", r"\1 COMMA", text)  # "نہیں، جمعہ" = correction
    toks = _clean(text).split()
    toks = _numbers_and_clock(toks, roman)
    toks = _periods(toks)
    mapped = [t for t in _map_tokens(toks, names) if t != ""]
    s = set(mapped)
    extra: list[str] = []
    booking_nouns = {"appointment", "appointments", "doctor", "nurse", "vaccine", "check", "lab", "physical", "shot"}
    if "__want__" in s and (s & booking_nouns or s & {"flu", "blood", "fever", "cough", "cold", "sick", "pain"}):
        extra.append("book appointment")
    if "change" in s and "appointment" in s and "reschedule" not in s:
        extra.append("reschedule appointment")
    if "__talk__" in s and s & {"real", "receptionist", "operator", "insaan", "person"}:
        extra.append("talk to a real person")
    first_q = next((q for q in ("when", "where", "how") if q in s), None)
    text_out = " ".join(t for t in mapped if not t.startswith("__"))
    if first_q:
        text_out = f"{first_q} {text_out}"
    elif "hours" in s or "address" in s or "parking" in s or "insurance" in s or "services" in s or "policy" in s:
        text_out = f"what {text_out}"
    return re.sub(r"\s+", " ", f"{text_out} {' '.join(extra)}").strip()
