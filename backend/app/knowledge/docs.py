"""Typed knowledge documents built from the clinic rules (YAML), in English and Urdu.

The YAML stays the source of truth. These documents are a *projection* of it: the same text is then loaded into SQL (state),
Elasticsearch (search) and Neo4j (relationships), so every answer can point back at where it came from.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.brain.urdu import UR_WEEKDAYS
from app.rules import ClinicRules

Kind = Literal["faq", "policy", "service", "provider"]
Lang = Literal["en", "ur"]
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class KDoc(BaseModel):
    doc_id: str  # "faq:hours@en": unique per language
    base_id: str  # "faq:hours": the same fact in every language (what retrieval is judged against)
    kind: Kind
    lang: Lang
    title: str
    text: str
    keywords: list[str] = Field(default_factory=list)
    refs: dict[str, list[str]] = Field(default_factory=dict)  # services / providers / policies this document is about
    source: str  # provenance, e.g. "clinic.yaml · policy.cancel_notice_hours"

    @field_validator("title", "text", "source")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @property
    def checksum(self) -> str:
        """Stable fingerprint of the content: ingestion skips documents whose checksum has not changed."""
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# Which services / policies each FAQ is about. This editorial mapping is what lets the graph answer "what else is relevant?".
FAQ_REFS: dict[str, dict[str, list[str]]] = {
    "hours": {"policies": ["holidays"]},
    "arrive": {"policies": ["arrive_early", "late_arrival"]},
    "cancellation": {"policies": ["cancel_notice"]},
    "new_patients": {"services": ["new_patient_visit"], "policies": ["verification"]},
    "services": {"services": ["*"]},
}


def _join(items: list[str], sep: str, last: str) -> str:
    if len(items) <= 1:
        return "".join(items)
    return sep.join(items[:-1]) + f" {last} " + items[-1]


def build_documents(rules: ClinicRules) -> list[KDoc]:
    docs: list[KDoc] = []
    pol, clinic = rules.policy, rules.clinic
    all_services = [s.key for s in rules.services]
    svc = {s.key: s for s in rules.services}

    # ---------------------------------------------------------------- FAQ (verbatim from the YAML)
    for f in rules.faq:
        refs = {k: (all_services if v == ["*"] else v) for k, v in FAQ_REFS.get(f.id, {}).items()}
        for lang, title, text in (
            ("en", f.question, f.answer),
            ("ur", f.question_ur or f.question, f.answer_ur or f.answer),
        ):
            docs.append(
                KDoc(
                    doc_id=f"faq:{f.id}@{lang}",
                    base_id=f"faq:{f.id}",
                    kind="faq",
                    lang=lang,
                    title=title,
                    text=text,
                    keywords=f.keywords if lang == "en" else [],
                    refs=refs,
                    source=f"clinic.yaml · faq.{f.id}",
                )
            )

    # ---------------------------------------------------------------- policies (numbers come from the YAML, never retyped)
    hrs = pol.min_lead_minutes // 60
    policies: list[tuple[str, str, str, str, str, dict[str, list[str]]]] = [
        (
            "cancel_notice",
            "Cancellation notice",
            f"Appointments can be cancelled or changed free of charge up to {pol.cancel_notice_hours} hours before the visit. "
            f"Changes inside {pol.cancel_notice_hours} hours count as late changes and a fee may apply.",
            f"اپائنٹمنٹ سے {pol.cancel_notice_hours} گھنٹے پہلے تک آپ مفت منسوخ یا تبدیل کر سکتے ہیں۔ "
            f"{pol.cancel_notice_hours} گھنٹوں کے اندر تبدیلی دیر سے تبدیلی شمار ہوتی ہے اور اس پر فیس لگ سکتی ہے۔",
            "policy.cancel_notice_hours",
            {"services": all_services},
        ),
        (
            "late_arrival",
            "Late arrival",
            f"If you arrive more than {pol.late_arrival_grace_minutes} minutes late, your appointment may need to be rescheduled.",
            f"اگر آپ {pol.late_arrival_grace_minutes} منٹ سے زیادہ دیر سے پہنچیں تو آپ کی اپائنٹمنٹ دوبارہ طے کرنی پڑ سکتی ہے۔",
            "policy.late_arrival_grace_minutes",
            {"services": all_services},
        ),
        (
            "arrive_early",
            "Arriving early",
            f"New patients should arrive {pol.arrive_early_new_minutes} minutes early to register. "
            f"Established patients should arrive {pol.arrive_early_established_minutes} minutes early.",
            f"نئے مریض رجسٹریشن کے لیے اپائنٹمنٹ سے {pol.arrive_early_new_minutes} منٹ پہلے پہنچیں۔ "
            f"پرانے مریض {pol.arrive_early_established_minutes} منٹ پہلے پہنچیں۔",
            "policy.arrive_early_*_minutes",
            {"services": all_services},
        ),
        (
            "min_lead",
            "Booking lead time",
            f"Appointments must be booked at least {hrs} hours ahead. Same-day visits inside that window are not offered online.",
            f"اپائنٹمنٹ کم از کم {hrs} گھنٹے پہلے بک کرنی ہوتی ہے۔ اس مدت کے اندر اسی دن کی وزٹ فون پر نہیں دی جاتی۔",
            "policy.min_lead_minutes",
            {"services": all_services},
        ),
        (
            "max_advance",
            "How far ahead you can book",
            f"You can book up to {pol.max_advance_days} days in advance.",
            f"آپ زیادہ سے زیادہ {pol.max_advance_days} دن پہلے تک اپائنٹمنٹ بک کر سکتے ہیں۔",
            "policy.max_advance_days",
            {"services": all_services},
        ),
        (
            "verification",
            "Identity verification",
            f"To protect your privacy we verify your full name and date of birth before booking or changing anything. "
            f"After {pol.max_verify_attempts} failed attempts the call is passed to our staff.",
            f"آپ کی رازداری کے لیے کوئی بھی تبدیلی کرنے سے پہلے ہم آپ کا پورا نام اور تاریخِ پیدائش پوچھتے ہیں۔ "
            f"{pol.max_verify_attempts} ناکام کوششوں کے بعد کال عملے کو منتقل کر دی جاتی ہے۔",
            "policy.max_verify_attempts",
            {},
        ),
        (
            "holidays",
            "Public holidays",
            "The clinic is closed on public holidays, and no appointments can be booked on those days.",
            "کلینک سرکاری تعطیلات پر بند رہتا ہے اور ان دنوں میں اپائنٹمنٹ بک نہیں کی جا سکتی۔",
            "holidays",
            {},
        ),
        (
            "emergency",
            "Emergencies",
            f"In an emergency call {clinic.emergency_en} straight away. This assistant is not an emergency service.",
            f"ہنگامی صورت میں فوراً {clinic.emergency_ur} پر کال کریں۔ یہ خودکار اسسٹنٹ ہنگامی خدمت نہیں ہے۔",
            "clinic.emergency_en",
            {},
        ),
    ]
    for key, title_en, en, ur, src, refs in policies:
        title_ur = {
            "cancel_notice": "منسوخی کی اطلاع",
            "late_arrival": "دیر سے آمد",
            "arrive_early": "جلدی پہنچنا",
            "min_lead": "بکنگ کا کم از کم وقت",
            "max_advance": "کتنے دن پہلے بک کریں",
            "verification": "شناخت کی تصدیق",
            "holidays": "سرکاری تعطیلات",
            "emergency": "ہنگامی صورتحال",
        }[key]
        for lang, title, text in (("en", title_en, en), ("ur", title_ur, ur)):
            docs.append(
                KDoc(
                    doc_id=f"policy:{key}@{lang}",
                    base_id=f"policy:{key}",
                    kind="policy",
                    lang=lang,
                    title=title,
                    text=text,
                    refs=refs,
                    source=f"clinic.yaml · {src}",
                )
            )

    # ---------------------------------------------------------------- services
    for s in rules.services:
        who_en = {
            "new": "for new patients only",
            "established": "for established patients",
            "any": "for new and established patients",
        }[s.requires]
        who_ur = {
            "new": "صرف نئے مریضوں کے لیے",
            "established": "پرانے مریضوں کے لیے",
            "any": "نئے اور پرانے دونوں مریضوں کے لیے",
        }[s.requires]
        providers = [p for p in rules.providers if s.key in p.services]
        pe = _join([f"{p.title} {p.name}" for p in providers], ", ", "and")
        pu = _join([f"{p.title_ur} {p.name_ur}" for p in providers], "، ", "اور")
        en = f"A {s.name} is a {s.duration_minutes}-minute visit {who_en}. Typical reasons: {', '.join(s.keywords[:6])}. Available with {pe}."
        ur = f"{s.name_ur} {s.duration_minutes} منٹ کی وزٹ ہے، {who_ur}۔ دستیاب معالج: {pu}۔"
        refs = {"providers": [p.key for p in providers], "services": [s.key]}
        for lang, title, text in (("en", s.name.capitalize(), en), ("ur", s.name_ur, ur)):
            docs.append(
                KDoc(
                    doc_id=f"service:{s.key}@{lang}",
                    base_id=f"service:{s.key}",
                    kind="service",
                    lang=lang,
                    title=title,
                    text=text,
                    keywords=s.keywords if lang == "en" else [],
                    refs=refs,
                    source=f"clinic.yaml · services.{s.key}",
                )
            )

    # ---------------------------------------------------------------- providers
    for p in rules.providers:
        days = [WEEKDAY_KEYS.index(d) for d in p.schedule]
        en_days = _join([EN_DAYS[i] for i in sorted(days)], ", ", "and")
        ur_days = _join([UR_WEEKDAYS[i] for i in sorted(days)], "، ", "اور")
        sv = [svc[k] for k in p.services]
        en = f"{p.title} {p.name} is a {p.specialty.lower()} provider. Services: {_join([x.name for x in sv], ', ', 'and')}. Clinic days: {en_days}."
        ur = f"{p.title_ur} {p.name_ur} درج ذیل خدمات فراہم کرتے ہیں: {_join([x.name_ur for x in sv], '، ', 'اور')}۔ دن: {ur_days}۔"
        refs = {"services": list(p.services), "providers": [p.key]}
        for lang, title, text in (
            ("en", f"{p.title} {p.name} · {p.specialty}", en),
            ("ur", f"{p.title_ur} {p.name_ur}", ur),
        ):
            docs.append(
                KDoc(
                    doc_id=f"provider:{p.key}@{lang}",
                    base_id=f"provider:{p.key}",
                    kind="provider",
                    lang=lang,
                    title=title,
                    text=text,
                    refs=refs,
                    source=f"clinic.yaml · providers.{p.key}",
                )
            )

    ids = [d.doc_id for d in docs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate knowledge document ids")
    return docs
