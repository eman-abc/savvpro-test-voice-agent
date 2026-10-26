import re

import pytest

from e2e.conftest import axe_violations

pytestmark = pytest.mark.e2e


def say(page, text):
    before = page.locator(".msg.bot").count()
    page.fill("#say", text)
    page.get_by_role("button", name="Send").click()
    page.wait_for_function("n => document.querySelectorAll('.msg.bot').length > n", arg=before)


def start_call(page, base_url):
    page.goto(base_url)
    page.get_by_role("button", name="Start a call").click()
    page.wait_for_selector(".msg.bot")


def book_flu_shot(page):
    for t in ["I need a flu shot", "Wei Zhang, May 17th 1995", "any provider", "next tuesday at 2 pm"]:
        say(page, t)


def go_to_block(page, text=None):
    """The calendar opens on the current week; step forward until an appointment (optionally with `text`) is visible."""
    for _ in range(4):
        page.wait_for_selector(".cal-grid")
        page.wait_for_function("() => !/Loading…/.test(document.body.innerText)")
        blocks = page.locator(".block", has_text=text) if text else page.locator(".block")
        if blocks.count():
            return blocks
        page.get_by_role("button", name="Next week").click()
        page.wait_for_timeout(400)
    raise AssertionError(f"no calendar block found for {text!r}")


def staff_login(page, base_url):
    page.goto(f"{base_url}/staff")
    page.get_by_role("button", name="Fill it in").click()
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_selector(".cal-grid")


def test_call_with_a_mid_call_correction_books_an_appointment(page, base_url):
    start_call(page, base_url)
    book_flu_shot(page)
    assert "2 PM" in page.locator(".slot.filled", has_text="Time").inner_text()
    say(page, "actually make it 3 pm")
    assert "3 PM" in page.locator(".slot.filled", has_text="Time").inner_text()
    assert page.get_by_text("Time corrected").count() >= 1  # the ledger shows the correction
    say(page, "yes")
    page.wait_for_selector(".appt-card")
    assert "Booked" in page.locator(".appt-card").inner_text()
    assert page.errors == []


def test_lost_response_is_retried_without_booking_twice(page, base_url):
    start_call(page, base_url)
    book_flu_shot(page)
    dropped = {"n": 0}

    def drop_first_yes(route):
        req = route.request
        if (
            req.method == "POST"
            and req.url.endswith("/turn")
            and '"yes"' in (req.post_data or "")
            and dropped["n"] == 0
        ):
            dropped["n"] += 1
            route.fetch()  # the server handles it...
            route.abort()  # ...but the browser never gets the answer
        else:
            route.continue_()

    page.route(re.compile(r".*/api/calls/.*/turn"), drop_first_yes)
    say(page, "yes")
    page.wait_for_selector(".appt-card", timeout=15000)
    assert dropped["n"] == 1
    staff_login(page, base_url)
    page.get_by_role("tab", name="Appointments").click()
    page.wait_for_selector("table.list tbody tr")  # rows, not just the table: the data loads after the tab opens
    texts = page.locator("table.list tbody tr", has_text="Wei Zhang").all_inner_texts()
    # this test booked 2 PM (the other test in this session booked 3 PM): exactly one 2 PM booking by the assistant
    mine = [t for t in texts if "2 PM" in t and "Voice" in t]
    assert len(mine) == 1, f"rows for Wei: {texts}"


def test_staff_can_see_and_cancel_an_appointment(page, base_url):
    staff_login(page, base_url)
    go_to_block(page, "Olivia").first.click()  # seeded a few days ahead, never in the past
    page.get_by_role("button", name="Cancel appointment").click()
    page.get_by_role("button", name="Yes, cancel it").click()
    page.wait_for_function("() => !document.querySelector('.detail')")


def test_call_log_shows_the_decision_trace(page, base_url):
    start_call(page, base_url)
    say(page, "what time do you open")
    staff_login(page, base_url)
    page.get_by_role("tab", name="Call log").click()
    page.locator(".calllist button").first.click()
    page.wait_for_selector(".transcript-static")
    assert "faq" in page.locator(".ledger").inner_text().lower()


def test_scenarios_tab_scores_100_percent(page, base_url):
    staff_login(page, base_url)
    page.get_by_role("tab", name="Scenarios").click()
    page.get_by_role("button", name="Run all scenarios").click()
    page.wait_for_selector(".stat", state="attached")
    page.wait_for_function("() => /100%/.test(document.body.innerText)")
    assert page.locator(".badge.bad").count() == 0


def test_wrong_staff_password_is_refused(page, base_url):
    page.goto(f"{base_url}/staff")
    page.fill("#u", "reception")
    page.fill("#p", "definitely-wrong")
    page.get_by_role("button", name="Sign in").click()
    assert "Incorrect username or password" in page.get_by_role("alert").inner_text()


def test_mobile_layout_does_not_scroll_sideways(browser, base_url):
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    pg = ctx.new_page()
    start_call(pg, base_url)
    say(pg, "book an appointment")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    pg.get_by_role("tab", name="Decisions").click()
    assert pg.locator(".turn").count() >= 2
    ctx.close()


# --------------------------------------------------------------------- accessibility ---
def test_accessibility_start_and_call_screens(page, base_url):
    page.goto(base_url)
    assert axe_violations(page) == []
    start_call(page, base_url)
    book_flu_shot(page)
    assert axe_violations(page) == []


def test_accessibility_staff_screens(page, base_url):
    page.goto(f"{base_url}/staff")
    assert axe_violations(page) == []
    staff_login(page, base_url)
    assert axe_violations(page) == []
    page.get_by_role("tab", name="Appointments").click()
    page.wait_for_selector("table.list")
    assert axe_violations(page) == []
    page.get_by_role("tab", name="Scenarios").click()
    page.get_by_role("button", name="Run all scenarios").click()
    page.wait_for_function("() => /100%/.test(document.body.innerText)")
    assert axe_violations(page) == []


# ------------------------------------------------------------------------------- Urdu ---
def test_urdu_call_in_the_browser_is_right_to_left_and_accessible(page, base_url):
    page.goto(base_url)
    page.get_by_role("radio", name="اردو").click()
    page.get_by_role("button", name="Start a call").click()
    page.wait_for_selector(".msg.bot")
    first = page.locator(".msg.bot").first
    assert "السلام علیکم" in first.inner_text()
    assert first.get_attribute("lang") == "ur" and first.get_attribute("dir") == "auto"
    say(page, "مجھے فلو کا ٹیکہ چاہیے")
    say(page, "میرا نام احمد خان ہے")
    say(page, "میری تاریخ پیدائش بارہ جون انیس سو تراسی ہے")
    assert "ویکسینیشن" in page.locator(".msg.bot").last.inner_text()
    assert page.get_by_role("button", name="کل صبح 10 بجے").count() == 1  # Urdu quick replies
    assert axe_violations(page) == []
    assert page.errors == []


def test_caller_who_speaks_urdu_in_an_english_call_is_answered_in_urdu(page, base_url):
    start_call(page, base_url)
    say(page, "mujhe kal subah 10 baje appointment chahiye")
    assert "شناخت" in page.locator(".msg.bot").last.inner_text()


# ------------------------------------------------------------- silence and two-way messages ---
def test_assistant_asks_if_the_caller_is_still_there_then_hangs_up(page, base_url):
    page.goto(f"{base_url}/?idle=1200")
    page.get_by_role("button", name="Start a call").click()
    page.wait_for_selector(".msg.bot")
    page.wait_for_function("() => /still there/.test(document.body.innerText)", timeout=8000)
    assert "Are you still there?" in page.locator(".msg.bot").last.inner_text()
    page.wait_for_function("() => /The call has ended/.test(document.body.innerText)", timeout=8000)
    assert page.get_by_role("button", name="Start a new call").count() == 1


def test_speaking_resets_the_idle_prompt(page, base_url):
    page.goto(f"{base_url}/?idle=2500")
    page.get_by_role("button", name="Start a call").click()
    page.wait_for_selector(".msg.bot")
    page.wait_for_function("() => /still there/.test(document.body.innerText)", timeout=8000)
    say(page, "what time do you open")
    assert "8 AM to 5 PM" in page.locator(".msg.bot").last.inner_text()
    assert not page.get_by_text("The call has ended").count()


def test_reminder_is_confirmed_by_a_patient_reply_in_the_staff_ui(page, base_url):
    staff_login(page, base_url)
    page.get_by_role("tab", name="Messages").click()
    page.get_by_role("button", name="Send due reminders now").click()
    page.wait_for_selector("table.list tbody tr")
    row = page.locator("table.list tbody tr", has_text="Maria Gonzalez").first
    assert "Reply 1 to confirm" in row.inner_text()
    row.get_by_role("button", name="1 · Confirm").click()
    page.wait_for_function(
        "() => /Patient receives: Thank you, your appointment is confirmed/.test(document.body.innerText)"
    )
    reminder = page.locator("table.list tbody tr", has_text="Maria Gonzalez").filter(has_text="Patient replied")
    assert reminder.locator(".badge.ok", has_text="confirmed").count() == 1, page.locator("table.list").inner_text()
    page.get_by_role("tab", name="Calendar").click()
    go_to_block(page, "Maria").first.click()
    assert "Yes" in page.locator(".detail", has_text="Patient confirmed").inner_text()


def test_accessibility_messages_and_waitlist_tabs(page, base_url):
    staff_login(page, base_url)
    page.get_by_role("tab", name="Messages").click()
    page.wait_for_selector(".toolbar")
    assert axe_violations(page) == []
    page.get_by_role("tab", name="Waitlist").click()
    page.wait_for_selector(".empty, table.list")
    assert axe_violations(page) == []
