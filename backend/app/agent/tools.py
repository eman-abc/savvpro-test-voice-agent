"""The tools the agent may call. Every one is a thin, validated wrapper over the same ClinicService the rules engine uses, so the
database, not the model, enforces hours, notice periods and double-booking.

Safety design (all deterministic):
- identity gate: appointment tools refuse until verify_patient / register_patient succeeded (state is set by code, never by the model);
- propose, then confirm: nothing is written by a propose_* tool; confirm_pending only runs if the proposal is from an EARLIER turn and
  the caller's latest message is a clear "yes" (checked here, not trusted from the model);
- the model never sees whether a name exists: failures are generic.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agent.context import TurnContext
from app.brain.text import yes_no
from app.services.clinic import BookingError, SlotTakenError, spread_pick


def _j(**kw: Any) -> str:
    return json.dumps(kw, ensure_ascii=False, default=str)


def spoken(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    minute = f":{dt.minute:02d}" if dt.minute else ""
    return f"{dt:%A}, {dt:%B} {dt.day} at {h}{minute} {'AM' if dt.hour < 12 else 'PM'}"


def build_tools(ctx: TurnContext) -> list[BaseTool]:
    svc, st, tr, rules = ctx.svc, ctx.st, ctx.tr, ctx.svc.rules
    services = {s.key: s for s in rules.services}

    def traced(kind_ok: str = "check") -> Callable:
        def deco(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> str:
                out = fn(*args, **kwargs)
                data = json.loads(out)
                refused = data.get("ok") is False
                tr.step(
                    "rule" if refused else kind_ok,
                    f"Tool {fn.__name__}" + (" refused" if refused else ""),
                    (data.get("error") or json.dumps(kwargs, ensure_ascii=False))[:160],
                )
                return out

            return wrapper

        return deco

    def need_verified() -> str | None:
        if not (st.verified and st.patient_id):
            return _j(
                ok=False,
                error="not_verified",
                hint="Verify the caller (verify_patient) or register them (register_patient) first.",
            )
        return None

    def end(reason: str, summary: str | None = None) -> None:
        st.ended, st.end_reason, st.step = True, reason, "ENDED"
        if summary:
            st.handoff_summary = summary

    def parse_start(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @tool
    @traced()
    def search_knowledge(question: str) -> str:
        """Look up clinic facts (hours, location, policies, services, providers, insurance). Answer ONLY from these results and cite nothing else.
        Returns matching facts with their source, related facts from the clinic knowledge graph, and services worth suggesting."""
        res = ctx.knowledge.ask(question, st.lang, k=3)
        if "faq" not in st.actions:
            st.actions.append("faq")
        return _j(
            ok=True,
            results=[{k: r[k] for k in ("title", "text", "source", "facts")} for r in res["results"]],
            suggestions=res["suggestions"],
        )

    @tool
    @traced()
    def list_services() -> str:
        """List the services the clinic offers, how long each takes, who can book it, and which providers offer it."""
        return _j(
            ok=True,
            services=[
                {
                    "key": s.key,
                    "name": s.name,
                    "minutes": s.duration_minutes,
                    "for": s.requires,
                    "providers": [p.key for p in rules.providers_for(s.key)],
                }
                for s in rules.services
            ],
        )

    @tool
    @traced()
    def verify_patient(full_name: str, date_of_birth: str) -> str:
        """Verify an existing patient before any appointment work. date_of_birth must be ISO YYYY-MM-DD. Never tell the caller which part failed."""
        if st.verified:
            return _j(ok=True, already_verified=True, first_name=st.patient_first)
        try:
            dob = date.fromisoformat(date_of_birth)
        except ValueError:
            return _j(ok=False, error="bad_date", hint="Ask for the date of birth again and pass it as YYYY-MM-DD.")
        if len(full_name.split()) < 2:
            return _j(ok=False, error="need_full_name", hint="Ask for the first and last name.")
        if svc.verification_locked(full_name):
            end("handoff", "identity verification locked after repeated failures")
            return _j(ok=False, error="locked", hint="Tell the caller you are transferring them to staff.")
        patient, _score = svc.find_patient(full_name, dob)
        if patient is None:
            st.verify["attempts"] = int(st.verify.get("attempts", 0)) + 1
            locked = svc.record_verification_failure(full_name, ctx.max_failures, ctx.lock_minutes)
            if locked or st.verify["attempts"] >= rules.policy.max_verify_attempts:
                end("handoff", "could not verify the caller's identity")
                return _j(
                    ok=False, error="not_verified_handoff", hint="Tell the caller you are transferring them to staff."
                )
            return _j(
                ok=False,
                error="not_verified",
                hint="Details did not match. Do not say which part was wrong; ask them to try again.",
            )
        svc.clear_verification_failures(full_name)
        st.verified, st.patient_id, st.patient_first = True, patient.id, patient.first_name
        st.established = svc.is_established(patient.id)
        return _j(
            ok=True,
            first_name=patient.first_name,
            established=st.established,
            upcoming_appointments=len(svc.upcoming(patient.id)),
        )

    @tool
    @traced()
    def register_patient(full_name: str, date_of_birth: str, phone: str | None = None) -> str:
        """Register a NEW patient (only when the caller says they are new). date_of_birth is ISO YYYY-MM-DD; phone is optional."""
        if st.verified:
            return _j(ok=False, error="already_verified")
        try:
            dob = date.fromisoformat(date_of_birth)
        except ValueError:
            return _j(ok=False, error="bad_date", hint="Pass the date of birth as YYYY-MM-DD.")
        parts = full_name.split()
        if len(parts) < 2:
            return _j(ok=False, error="need_full_name")
        if (
            svc.find_patient(full_name, dob)[0] is not None
        ):  # never hand out an existing patient's record to a "new" caller
            end("handoff", "a caller registering as new matches an existing record")
            return _j(ok=False, error="cannot_register", hint="Tell the caller you are transferring them to staff.")
        p = svc.create_patient(parts[0], " ".join(parts[1:]), dob, (phone or "").strip() or None)
        st.verified, st.patient_id, st.patient_first, st.established = True, p.id, p.first_name, False
        return _j(ok=True, first_name=p.first_name, established=False)

    @tool
    @traced()
    def find_availability(
        service: str, date: str | None = None, provider: str | None = None, part_of_day: str | None = None
    ) -> str:
        """Find open appointment times. service is a key from list_services. date is ISO YYYY-MM-DD (omit for the earliest openings).
        provider is a provider key or omitted for any. part_of_day is 'morning' or 'afternoon' or omitted. Returns up to 4 options."""
        if service not in services:
            return _j(ok=False, error="unknown_service", hint="Call list_services for valid keys.")
        if provider and provider not in {p.key for p in rules.providers_for(service)}:
            return _j(ok=False, error="provider_does_not_offer_service")
        if date:
            try:
                day = datetime.fromisoformat(date).date()
            except ValueError:
                return _j(ok=False, error="bad_date", hint="Pass the date as YYYY-MM-DD.")
            closed = svc.validate_day(day, provider)
            if closed:
                return _j(ok=False, error="unavailable_day", hint=str(closed))
            slots = svc.slots_for_day(day, service, provider)
        else:
            slots = svc.find_earliest(service, provider, None, 6)
        if part_of_day == "morning":
            slots = [s for s in slots if s.start.hour < 12]
        elif part_of_day == "afternoon":
            slots = [s for s in slots if s.start.hour >= 12]
        picked = spread_pick(slots, 4) if date else slots[:4]
        ctx.options = [
            {"provider": s.provider_key, "start": s.start.isoformat(), "when": spoken(s.start)} for s in picked
        ]
        st.slots = {"service": service, "provider": provider or "any", "date": date}
        return (
            _j(ok=True, options=[{**o, "provider_name": rules.provider(o["provider"]).name} for o in ctx.options])
            if picked
            else _j(
                ok=True,
                options=[],
                hint="Nothing open then. Offer another day, or the waitlist is not available in this mode: offer a person.",
            )
        )

    @tool
    @traced()
    def list_my_appointments() -> str:
        """List the verified caller's upcoming appointments (with ids needed to cancel or reschedule)."""
        if refusal := need_verified():
            return refusal
        return _j(
            ok=True,
            appointments=[
                {
                    "id": a.id,
                    "service": a.service_key,
                    "provider": a.provider.key,
                    "start": a.start_at.isoformat(),
                    "when": spoken(a.start_at),
                }
                for a in svc.upcoming(st.patient_id)
            ],
        )

    def _stage(action: str, summary: str, **fields: Any) -> str:
        st.pending = {"action": action, "turn": st.turn, "summary": summary, **fields}
        return _j(
            ok=True,
            summary=summary,
            next="Read this back and ask the caller to confirm. Do NOT call confirm_pending until they answer in their next message.",
            **fields,
        )

    def _owned(appointment_id: int) -> Any | str:
        a = svc.get_appointment(appointment_id)
        if a is None or a.patient_id != st.patient_id or a.status != "booked":
            return _j(ok=False, error="appointment_not_found")
        return a

    @tool
    @traced()
    def propose_booking(service: str, provider: str, start: str) -> str:
        """Stage a booking for the caller to confirm. Does NOT book. start is ISO YYYY-MM-DDTHH:MM taken from find_availability options."""
        if refusal := need_verified():
            return refusal
        s, when = services.get(service), parse_start(start)
        if s is None or when is None or provider not in {p.key for p in rules.providers_for(service)}:
            return _j(
                ok=False,
                error="invalid_request",
                hint="Use a service key, a provider that offers it, and an ISO start time from find_availability.",
            )
        if s.requires == "new" and st.established or s.requires == "established" and not st.established:
            return _j(
                ok=False, error="service_not_available_to_this_patient", hint=f"{s.name} is for {s.requires} patients."
            )
        if svc.validate_day(when.date(), provider) or when not in svc.starts_for_day(provider, when.date(), service):
            return _j(ok=False, error="slot_unavailable", hint="Call find_availability again.")
        from datetime import timedelta

        if not svc.patient_is_free(st.patient_id, when, when + timedelta(minutes=s.duration_minutes)):
            return _j(ok=False, error="patient_already_booked_then")
        p = rules.provider(provider)
        return _stage(
            "book",
            f"{s.name} with {p.title} {p.name} on {spoken(when)}",
            service=service,
            provider=provider,
            start=when.isoformat(),
        )

    @tool
    @traced()
    def propose_cancel(appointment_id: int) -> str:
        """Stage a cancellation for the caller to confirm. Does NOT cancel. Warns if it is inside the late-change window (a fee may apply)."""
        if refusal := need_verified():
            return refusal
        a = _owned(appointment_id)
        if isinstance(a, str):
            return a
        late = svc.is_late_change(a)
        return _stage(
            "cancel",
            f"cancel {services[a.service_key].name} on {spoken(a.start_at)}",
            appointment_id=a.id,
            late_change=late,
            fee_warning=(
                "This is inside the notice window, so a late-change fee may apply. Say so before asking."
                if late
                else None
            ),
        )

    @tool
    @traced()
    def propose_reschedule(appointment_id: int, new_start: str, provider: str | None = None) -> str:
        """Stage moving an appointment to a new time for the caller to confirm. Does NOT move it. new_start is ISO from find_availability."""
        if refusal := need_verified():
            return refusal
        a = _owned(appointment_id)
        if isinstance(a, str):
            return a
        when, prov = parse_start(new_start), provider or a.provider.key
        if when is None or prov not in {p.key for p in rules.providers_for(a.service_key)}:
            return _j(ok=False, error="invalid_request")
        if svc.validate_day(when.date(), prov) or when not in svc.starts_for_day(prov, when.date(), a.service_key):
            return _j(ok=False, error="slot_unavailable", hint="Call find_availability again.")
        p = rules.provider(prov)
        return _stage(
            "reschedule",
            f"move {services[a.service_key].name} to {spoken(when)} with {p.title} {p.name}",
            appointment_id=a.id,
            provider=prov,
            start=when.isoformat(),
            late_change=svc.is_late_change(a),
        )

    @tool
    @traced("check")
    def confirm_pending() -> str:
        """Carry out the staged booking, cancellation or reschedule. Only works if it was staged in an earlier turn and the caller's latest
        message is a clear yes. Never call it in the same turn as a propose_* tool."""
        p = st.pending
        if not p or "action" not in p:
            return _j(ok=False, error="nothing_pending")
        if int(p["turn"]) >= st.turn:
            return _j(
                ok=False,
                error="needs_caller_confirmation",
                hint="Ask the caller to confirm first; they must answer in their next message.",
            )
        if yes_no(ctx.norm) != "yes":
            return _j(
                ok=False,
                error="caller_did_not_confirm",
                hint="The latest message is not a clear yes. Do not proceed; ask again or offer to change it.",
            )
        try:
            if p["action"] == "book":
                a = svc.book(
                    st.patient_id, p["provider"], p["service"], datetime.fromisoformat(p["start"]), ctx.actor, "agent"
                )
                st.actions.append("booked")
                verb = "booked"
            elif p["action"] == "reschedule":
                a = svc.reschedule(
                    int(p["appointment_id"]), p["provider"], datetime.fromisoformat(p["start"]), ctx.actor
                )
                st.actions.append("rescheduled")
                verb = "rescheduled"
            else:
                a = svc.cancel(int(p["appointment_id"]), ctx.actor)
                st.actions.append("cancelled")
                verb = "cancelled"
        except SlotTakenError:
            st.pending = {}
            return _j(ok=False, error="slot_just_taken", hint="Apologise and call find_availability for new options.")
        except BookingError as exc:
            st.pending = {}
            return _j(ok=False, error="rule_violation", hint=str(exc))
        st.pending = {}
        ctx.appointment = {
            "id": a.id,
            "action": verb,
            "description": f"{services[a.service_key].name} with {rules.provider(a.provider.key).title} {a.provider.name} on {spoken(a.start_at)}",
            "start": a.start_at.isoformat(),
            "end": a.end_at.isoformat(),
            "service": a.service_key,
            "service_name": services[a.service_key].name,
            "provider": a.provider.key,
            "provider_name": f"{rules.provider(a.provider.key).title} {a.provider.name}",
        }
        return _j(ok=True, result=verb, when=spoken(a.start_at))

    @tool
    @traced()
    def handoff_to_staff(reason: str) -> str:
        """Transfer the caller to a human: when they ask for one, are frustrated, or you cannot help after two tries. Ends the call."""
        end("handoff", reason[:300])
        return _j(ok=True, hint="Tell the caller a member of staff will take over, then stop.")

    @tool
    @traced()
    def end_call() -> str:
        """End the call politely once the caller says they are finished."""
        end("goodbye")
        return _j(ok=True)

    return [
        search_knowledge,
        list_services,
        verify_patient,
        register_patient,
        find_availability,
        list_my_appointments,
        propose_booking,
        propose_cancel,
        propose_reschedule,
        confirm_pending,
        handoff_to_staff,
        end_call,
    ]
