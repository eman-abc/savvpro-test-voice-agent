import threading
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import models as m
from app.db import Database
from app.models import Base
from app.seed import seed_demo
from app.services.clinic import (
    ClinicService,
    PatientConflictError,
    SlotTakenError,
    TimeWindow,
    spread_pick,
)
from tests.conftest import NOW, make_settings

TUE = date(2026, 10, 27)
NEXT_TUE = date(2026, 11, 3)


def patient(svc, first):
    return svc.db.scalar(select(m.Patient).where(m.Patient.first_name == first))


def test_slots_respect_hours_lunch_and_lead_time(svc):
    starts = svc.starts_for_day("patel", NEXT_TUE, "general_consultation")
    times = {s.time() for s in starts}
    assert time(8, 0) in times and time(11, 30) in times
    assert time(12, 0) not in times and time(12, 30) not in times  # lunch break 12-13
    assert time(11, 45) not in times  # 30 min visit would run into lunch
    assert time(16, 30) in times and time(16, 45) not in times  # clinic closes at 17:00
    # today is Monday 10:00 with a 2h lead: nothing before 12:00 is offered
    today = svc.starts_for_day("patel", NOW.date(), "general_consultation")
    assert today and min(today) >= NOW + timedelta(hours=2)


def test_closed_days_holidays_and_range(svc):
    assert svc.validate_day(date(2026, 11, 1)) == "closed_day"  # Sunday
    assert svc.validate_day(date(2026, 11, 26)) == "holiday"  # Thanksgiving
    assert svc.validate_day(date(2026, 10, 20)) == "past"
    assert svc.validate_day(date(2027, 6, 1)) == "too_far"
    assert svc.validate_day(date(2026, 10, 28), "nguyen") == "provider_off"  # Dr. Nguyen is not in on Wednesdays
    assert svc.validate_day(date(2026, 10, 28), "patel") is None


def test_provider_must_offer_service(svc):
    assert svc.starts_for_day("patel", NEXT_TUE, "lab_work") == []


def test_booking_blocks_overlapping_times(svc):
    p = patient(svc, "Wei")
    start = datetime(2026, 11, 3, 9, 0)
    appt = svc.book(p.id, "patel", "general_consultation", start, actor="t")  # 30 min = two 15-min slots
    assert appt.end_at == start + timedelta(minutes=30)
    free = svc.starts_for_day("patel", NEXT_TUE, "general_consultation")
    assert datetime(2026, 11, 3, 9, 0) not in free and datetime(2026, 11, 3, 9, 15) not in free
    assert datetime(2026, 11, 3, 9, 30) in free
    # a short visit may not start inside the booked half hour either
    assert datetime(2026, 11, 3, 9, 15) not in svc.starts_for_day("patel", NEXT_TUE, "vaccination")


def test_second_booking_of_same_slot_is_refused(svc):
    a, b = patient(svc, "Wei"), patient(svc, "Jose")
    start = datetime(2026, 11, 3, 10, 0)
    svc.book(a.id, "patel", "general_consultation", start, actor="t")
    with pytest.raises(SlotTakenError):
        svc.book(b.id, "patel", "general_consultation", start, actor="t")


def test_database_constraint_itself_blocks_double_booking(svc):
    """Even if application checks were bypassed, (provider, slot) is UNIQUE."""
    a = patient(svc, "Wei")
    start = datetime(2026, 11, 3, 14, 0)
    prov = svc.provider_row("patel")
    for _ in range(2):
        appt = m.Appointment(
            patient_id=a.id,
            provider_id=prov.id,
            service_key="vaccination",
            start_at=start,
            end_at=start + timedelta(minutes=15),
        )
        appt.slots = [m.AppointmentSlot(provider_id=prov.id, slot_start=start)]
        svc.db.add(appt)
        if _ == 0:
            svc.db.flush()
    with pytest.raises(IntegrityError):
        svc.db.flush()
    svc.db.rollback()


def test_concurrent_race_has_exactly_one_winner(tmp_path, rules):
    """Two threads, two sessions, same slot: the database lets exactly one through."""
    db = Database(make_settings(database_url=f"sqlite:///{tmp_path / 'race.db'}"))
    Base.metadata.create_all(db.engine)
    with db.session() as s:
        boot = ClinicService(s, rules, now_fn=lambda: NOW)
        boot.sync_providers()
        seed_demo(boot, NOW)
        ids = [p.id for p in s.scalars(select(m.Patient).where(m.Patient.first_name.in_(["Wei", "Jose"])))]
    start = datetime(2026, 11, 4, 9, 0)
    barrier, results = threading.Barrier(2), []

    def worker(pid: int):
        with db.session() as s:
            svc = ClinicService(s, rules, now_fn=lambda: NOW)
            barrier.wait()
            try:
                svc.book(pid, "patel", "general_consultation", start, actor="race")
                results.append("ok")
            except SlotTakenError:
                results.append("taken")

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok", "taken"]


def test_patient_cannot_hold_two_overlapping_appointments(svc):
    p = patient(svc, "Wei")
    svc.book(p.id, "patel", "general_consultation", datetime(2026, 11, 3, 9, 0), actor="t")
    with pytest.raises(PatientConflictError):
        svc.book(p.id, "nguyen", "general_consultation", datetime(2026, 11, 3, 9, 15), actor="t")


def test_cannot_book_outside_availability(svc):
    p = patient(svc, "Wei")
    with pytest.raises(SlotTakenError):
        svc.book(p.id, "patel", "general_consultation", datetime(2026, 11, 3, 12, 0), actor="t")  # lunch
    with pytest.raises(SlotTakenError):
        svc.book(p.id, "patel", "general_consultation", datetime(2026, 11, 1, 9, 0), actor="t")  # Sunday


def test_reschedule_moves_atomically_and_can_shift_by_15_minutes(svc):
    p = patient(svc, "Wei")
    a = svc.book(p.id, "patel", "general_consultation", datetime(2026, 11, 3, 9, 0), actor="t")
    moved = svc.reschedule(a.id, "patel", datetime(2026, 11, 3, 9, 15), actor="t")  # overlaps its own old slots
    assert moved.start_at == datetime(2026, 11, 3, 9, 15) and moved.reschedule_count == 1
    # the old 9:00 start is released (a 15-minute visit fits there again; a 30-minute one would hit 9:15)
    assert datetime(2026, 11, 3, 9, 0) in svc.starts_for_day("patel", NEXT_TUE, "vaccination")


def test_failed_reschedule_leaves_original_untouched(svc):
    a, b = patient(svc, "Wei"), patient(svc, "Jose")
    mine = svc.book(a.id, "patel", "general_consultation", datetime(2026, 11, 3, 9, 0), actor="t")
    svc.book(b.id, "patel", "general_consultation", datetime(2026, 11, 3, 10, 0), actor="t")
    with pytest.raises(SlotTakenError):
        svc.reschedule(mine.id, "patel", datetime(2026, 11, 3, 10, 0), actor="t")
    svc.db.expire_all()
    again = svc.get_appointment(mine.id)
    assert again.start_at == datetime(2026, 11, 3, 9, 0) and again.status == "booked"
    assert datetime(2026, 11, 3, 9, 0) not in svc.starts_for_day("patel", NEXT_TUE, "general_consultation")


def test_cancel_frees_slot_flags_late_and_is_idempotent(svc):
    liam = patient(svc, "Liam")
    appt = svc.upcoming(liam.id)[0]  # seeded inside the 24h window
    assert svc.is_late_change(appt)
    start = appt.start_at
    done = svc.cancel(appt.id, actor="t")
    assert done.status == "cancelled" and done.late_change is True
    assert start in svc.starts_for_day("patel", start.date(), "general_consultation") or start < NOW + timedelta(
        hours=2
    )
    assert svc.cancel(appt.id, actor="t").status == "cancelled"
    assert svc.upcoming(liam.id) == []


def test_established_status_comes_from_past_visits(svc):
    assert svc.is_established(patient(svc, "Maria").id) is True
    assert svc.is_established(patient(svc, "Aisha").id) is False


def test_find_patient_is_fuzzy_on_name_but_exact_on_dob(svc):
    maria, score = svc.find_patient("Maria Gonzales", date(1985, 3, 14))
    assert maria and maria.last_name == "Gonzalez" and score >= 82
    assert svc.find_patient("Maria Gonzalez", date(1985, 3, 15))[0] is None
    assert svc.find_patient("Somebody Else", date(1985, 3, 14))[0] is None
    assert svc.find_patient("jose hernandez", date(1988, 4, 21))[0] is not None  # accents ignored


def test_load_balancing_between_providers(svc):
    slots = svc.slots_for_day(NEXT_TUE, "general_consultation", None)
    assert [s.start for s in slots] == sorted({s.start for s in slots})  # one entry per start time
    assert {s.provider_key for s in slots} >= {"patel", "nguyen"}
    # once Dr. Patel has a booking that day, a start both doctors could take goes to the lighter day
    svc.book(patient(svc, "Wei").id, "patel", "general_consultation", datetime(2026, 11, 3, 8, 30), actor="t")
    eleven = next(
        s
        for s in svc.slots_for_day(NEXT_TUE, "general_consultation", None)
        if s.start.hour == 11 and s.start.minute == 0
    )
    assert eleven.provider_key != "patel"


def test_earliest_and_window_filtering(svc):
    found = svc.find_earliest("vaccination", None, None, 3)
    assert len(found) == 3 and found[0].start < found[1].start < found[2].start
    afternoon = svc.slots_for_day(NEXT_TUE, "annual_physical", "patel", TimeWindow(time(13), time(17)))
    assert afternoon and all(time(13) <= s.start.time() <= time(17) for s in afternoon)


def test_spread_pick():
    from app.services.clinic import Slot

    slots = [Slot("patel", datetime(2026, 11, 3, 8, 0) + timedelta(minutes=15 * i)) for i in range(20)]
    picked = spread_pick(slots, 3)
    assert picked[0] == slots[0] and picked[-1] == slots[-1] and len(picked) == 3
