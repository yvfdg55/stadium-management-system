from datetime import datetime

import pytest

import app as stadium
from conftest import add_client, db_query, insert_booking, login_as_admin, post_form, recurring_payload


@pytest.fixture(autouse=True)
def fixed_stadium_time(monkeypatch):
    # Monday 2025-01-06 at noon; the default work cycle starts at 10:00.
    monkeypatch.setattr(stadium, "app_now", lambda: datetime(2025, 1, 6, 12, 0))


def test_recurring_form_renders_for_admin(client):
    login_as_admin(client)
    assert client.get("/admin/recurring-bookings/new").status_code == 200


def test_creating_rule_materializes_52_future_occurrences(client):
    login_as_admin(client)
    customer = add_client()
    response = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    assert response.status_code == 302
    rules = db_query("SELECT id, active FROM recurring_booking_rules")
    bookings = db_query("SELECT * FROM bookings WHERE booking_type='recurring'")
    assert len(rules) == 1
    assert rules[0]["active"] == 1
    assert len(bookings) == 52
    assert len({row["booking_date"] for row in bookings}) == 52
    assert all(row["weekly_rule_id"] == rules[0]["id"] for row in bookings)


def test_zero_rate_and_zero_deposit_are_valid_for_weekly_rule(client):
    login_as_admin(client)
    customer = add_client()
    response = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer, hourly_rate=0, deposit=0))
    assert response.status_code == 302
    rule = db_query("SELECT hourly_rate, deposit FROM recurring_booking_rules")[0]
    assert rule == {"hourly_rate": 0, "deposit": 0}


@pytest.mark.parametrize("field,value", [
    ("client_id", ""), ("weekday", "7"), ("weekday", "-1"), ("weekday", "abc"),
    ("start_time", "25:00"), ("start_time", "14:15"), ("duration_hours", "0"),
    ("duration_hours", "abc"), ("hourly_rate", "-1"), ("deposit", "-1"),
])
def test_invalid_weekly_rule_inputs_are_rejected(client, field, value):
    login_as_admin(client)
    customer = add_client()
    payload = recurring_payload(customer)
    payload[field] = value
    response = post_form(client, "/admin/recurring-bookings/new", payload)
    assert response.status_code == 400
    assert db_query("SELECT * FROM recurring_booking_rules") == []


def test_rule_rejects_nonexistent_client(client):
    login_as_admin(client)
    response = post_form(client, "/admin/recurring-bookings/new", recurring_payload(9999))
    assert response.status_code == 409
    assert db_query("SELECT * FROM recurring_booking_rules") == []


def test_overlapping_weekly_rules_are_rejected(client):
    login_as_admin(client)
    customer_a = add_client("أ", "1")
    customer_b = add_client("ب", "2")
    first = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer_a, start_time="14:00", duration_hours=2))
    assert first.status_code == 302
    overlapping = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer_b, start_time="15:00", duration_hours=1))
    assert overlapping.status_code == 409
    adjacent = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer_b, start_time="16:00", duration_hours=1))
    assert adjacent.status_code == 302
    assert len(db_query("SELECT id FROM recurring_booking_rules")) == 2


def test_weekly_rule_conflicts_with_future_single_booking(client):
    login_as_admin(client)
    customer = add_client()
    insert_booking(customer, booking_date="2025-01-06", start_time="14:00", duration=1)
    response = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer, weekday=0, start_time="14:00"))
    assert response.status_code == 409
    assert db_query("SELECT * FROM recurring_booking_rules") == []


def test_single_booking_is_blocked_by_weekly_rule_even_outside_generated_horizon(client):
    login_as_admin(client)
    customer = add_client()
    response = post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer, weekday=0, start_time="14:00"))
    assert response.status_code == 302
    # The direct validator protects any future matching weekday, not just generated rows.
    payload = {
        "client_id": str(customer), "booking_date": "2027-01-04", "start_time": "14:00",
        "hourly_rate": "100", "duration_hours": "1", "initial_payment": "0", "notes": "",
    }
    blocked = post_form(client, "/admin/bookings/new", payload)
    assert blocked.status_code == 409


def test_stop_preserves_generated_appointments_and_resume_does_not_duplicate(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    before = len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=?", (rule_id,)))
    assert before == 52
    stopped = post_form(client, f"/admin/recurring-bookings/{rule_id}/stop")
    assert stopped.status_code == 302
    assert db_query("SELECT active FROM recurring_booking_rules WHERE id=?", (rule_id,))[0]["active"] == 0
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=?", (rule_id,))) == before
    resumed = post_form(client, f"/admin/recurring-bookings/{rule_id}/resume")
    assert resumed.status_code == 302
    assert db_query("SELECT active FROM recurring_booking_rules WHERE id=?", (rule_id,))[0]["active"] == 1
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=?", (rule_id,))) == before


def test_update_weekly_rate_and_notes_updates_future_generated_bookings(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    response = post_form(client, f"/admin/recurring-bookings/{rule_id}/edit", recurring_payload(customer, hourly_rate=150, notes="ملاحظة جديدة"))
    assert response.status_code == 302
    rows = db_query("SELECT hourly_rate, total, notes FROM bookings WHERE weekly_rule_id=? AND status='active'", (rule_id,))
    assert len(rows) == 52
    assert all(row == {"hourly_rate": 150, "total": 150, "notes": "ملاحظة جديدة"} for row in rows)


def test_weekly_rate_cannot_be_lowered_below_existing_payment(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer, hourly_rate=100))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    booking_id = db_query("SELECT id FROM bookings WHERE weekly_rule_id=? ORDER BY booking_date LIMIT 1", (rule_id,))[0]["id"]
    with stadium.app.app_context():
        db = stadium.get_db()
        db.execute("INSERT INTO booking_payments (booking_id, amount) VALUES (?, 90)", (booking_id,))
        db.commit()
    response = post_form(client, f"/admin/recurring-bookings/{rule_id}/edit", recurring_payload(customer, hourly_rate=50))
    assert response.status_code == 409
    assert db_query("SELECT hourly_rate FROM recurring_booking_rules WHERE id=?", (rule_id,))[0]["hourly_rate"] == 100


def test_paid_future_occurrence_is_preserved_when_rule_pattern_changes(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    future = db_query("SELECT id, booking_date FROM bookings WHERE weekly_rule_id=? AND booking_date > '2025-01-06' ORDER BY booking_date LIMIT 1", (rule_id,))[0]
    with stadium.app.app_context():
        db = stadium.get_db()
        db.execute("INSERT INTO booking_payments (booking_id, amount) VALUES (?, 20)", (future["id"],))
        db.commit()
    changed = post_form(client, f"/admin/recurring-bookings/{rule_id}/edit", recurring_payload(customer, weekday=1, start_time="15:00"))
    assert changed.status_code == 302
    preserved = db_query("SELECT weekly_rule_id, booking_type, status FROM bookings WHERE id=?", (future["id"],))[0]
    assert preserved == {"weekly_rule_id": None, "booking_type": "single", "status": "active"}
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=? AND status='active'", (rule_id,))) >= 51


def test_todays_occurrence_is_detached_when_weekly_pattern_changes(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    today = db_query("SELECT id FROM bookings WHERE weekly_rule_id=? AND booking_date='2025-01-06'", (rule_id,))[0]
    changed = post_form(client, f"/admin/recurring-bookings/{rule_id}/edit", recurring_payload(customer, weekday=1, start_time="15:00"))
    assert changed.status_code == 302
    preserved = db_query("SELECT weekly_rule_id, booking_type, status FROM bookings WHERE id=?", (today["id"],))[0]
    assert preserved == {"weekly_rule_id": None, "booking_type": "single", "status": "active"}


def test_editing_one_weekly_occurrence_does_not_edit_the_series(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    occurrence = db_query("SELECT id, booking_date FROM bookings WHERE weekly_rule_id=? ORDER BY booking_date LIMIT 1", (rule_id,))[0]
    response = post_form(client, f"/admin/bookings/{occurrence['id']}/edit", {
        "client_id": str(customer), "booking_date": occurrence["booking_date"], "start_time": "15:00",
        "hourly_rate": "100", "duration_hours": "1", "initial_payment": "0", "notes": "تعديل مناسبة واحدة",
    })
    assert response.status_code == 302
    changed = db_query("SELECT start_time, weekly_rule_id, booking_type FROM bookings WHERE id=?", (occurrence["id"],))[0]
    assert changed == {"start_time": "15:00", "weekly_rule_id": rule_id, "booking_type": "single"}
    assert db_query("SELECT start_time FROM recurring_booking_rules WHERE id=?", (rule_id,))[0]["start_time"] == "14:00"


def test_weekly_occurrence_cannot_be_moved_to_another_date(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    occurrence = db_query("SELECT id, booking_date FROM bookings ORDER BY booking_date LIMIT 1")[0]
    response = post_form(client, f"/admin/bookings/{occurrence['id']}/edit", {
        "client_id": str(customer), "booking_date": "2025-02-01", "start_time": "14:00",
        "hourly_rate": "100", "duration_hours": "1", "initial_payment": "0", "notes": "",
    })
    assert response.status_code == 400
    assert db_query("SELECT booking_date FROM bookings WHERE id=?", (occurrence["id"],))[0]["booking_date"] == occurrence["booking_date"]


def test_recurring_deposit_collect_refund_and_duplicate_refund_guard(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer, deposit=75))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    booking_id = db_query("SELECT id FROM bookings WHERE weekly_rule_id=? ORDER BY booking_date LIMIT 1", (rule_id,))[0]["id"]
    assert db_query("SELECT transaction_type, amount FROM recurring_deposit_transactions WHERE rule_id=?", (rule_id,)) == [{"transaction_type": "collect", "amount": 75}]
    refund = post_form(client, f"/admin/bookings/{booking_id}/deposit/refund", {"payment_method": "transfer", "note": "إرجاع"})
    assert refund.status_code == 302
    transactions = db_query("SELECT transaction_type, amount, payment_method FROM recurring_deposit_transactions WHERE rule_id=? ORDER BY id", (rule_id,))
    assert transactions[-1] == {"transaction_type": "refund", "amount": 75, "payment_method": "transfer"}
    duplicate = post_form(client, f"/admin/bookings/{booking_id}/deposit/refund")
    assert duplicate.status_code == 302
    assert len(db_query("SELECT id FROM recurring_deposit_transactions WHERE transaction_type='refund'")) == 1


def test_invalid_refund_method_is_not_recorded(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer, deposit=30))
    booking_id = db_query("SELECT id FROM bookings ORDER BY id LIMIT 1")[0]["id"]
    response = post_form(client, f"/admin/bookings/{booking_id}/deposit/refund", {"payment_method": "crypto"})
    assert response.status_code == 302
    assert db_query("SELECT * FROM recurring_deposit_transactions WHERE transaction_type='refund'") == []


def test_rule_listing_and_edit_missing_rule_are_handled(client):
    login_as_admin(client)
    response = client.get("/admin/recurring-bookings/new")
    assert response.status_code == 200
    missing = client.get("/admin/recurring-bookings/999/edit")
    assert missing.status_code == 302


def test_recurrence_materialization_is_idempotent(client):
    login_as_admin(client)
    customer = add_client()
    post_form(client, "/admin/recurring-bookings/new", recurring_payload(customer))
    rule_id = db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    with stadium.app.app_context():
        db = stadium.get_db()
        first = stadium.materialize_recurring_rule(db, rule_id)
        second = stadium.materialize_recurring_rule(db, rule_id)
        db.commit()
    assert first == 0
    assert second == 0
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=?", (rule_id,))) == 52


def test_weekly_rule_list_filters_by_client_name_and_state(client):
    login_as_admin(client)
    customer_a=add_client(name="أحمد الزيود",phone="0792000001")
    customer_b=add_client(name="ليلى عمران",phone="0792000002")
    post_form(client,"/admin/recurring-bookings/new",recurring_payload(customer_a,start_time="14:00"))
    post_form(client,"/admin/recurring-bookings/new",recurring_payload(customer_b,start_time="16:00"))
    rule_b=db_query("SELECT id FROM recurring_booking_rules WHERE client_id=?",(customer_b,))[0]["id"]
    post_form(client,f"/admin/recurring-bookings/{rule_b}/stop")
    with stadium.app.app_context():
        active=stadium.get_recurring_booking_rules("أحمد","active")
        stopped=stadium.get_recurring_booking_rules("عمران","stopped")
    assert len(active)==1 and active[0]["client_name"]=="أحمد الزيود"
    assert len(stopped)==1 and stopped[0]["client_name"]=="ليلى عمران"
    page=client.get("/admin/recurring-bookings/new?client_filter=أحمد&rule_status=active")
    body=page.get_data(as_text=True)
    listed=body.split('<ul class="recurring-rules-list">',1)[1].split("</ul>",1)[0]
    assert page.status_code==200 and "أحمد الزيود" in listed and "ليلى عمران" not in listed
    assert "عدد النتائج: 1" in body and 'name="client_filter"' in body
    stopped_page=client.get("/admin/recurring-bookings/new?client_filter=عمران&rule_status=stopped")
    stopped_body=stopped_page.get_data(as_text=True)
    stopped_list=stopped_body.split('<ul class="recurring-rules-list">',1)[1].split("</ul>",1)[0]
    assert "ليلى عمران" in stopped_list and "أحمد الزيود" not in stopped_list


def test_hard_delete_unpaid_weekly_rule_removes_its_occurrences_only(client):
    login_as_admin(client)
    customer=add_client()
    post_form(client,"/admin/recurring-bookings/new",recurring_payload(customer,hourly_rate=0,deposit=0))
    rule_id=db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    insert_booking(customer,booking_date="2025-01-06",start_time="17:00")
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("UPDATE bookings SET status='cancelled' WHERE weekly_rule_id=? AND booking_date='2025-01-13'",(rule_id,)); db.commit()
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=?",(rule_id,)))==52
    assert client.get(f"/admin/recurring-bookings/{rule_id}/delete").status_code==405
    response=post_form(client,f"/admin/recurring-bookings/{rule_id}/delete",{"client_filter":"عميل اختبار","rule_status":"active"})
    assert response.status_code==302 and "client_filter" in response.headers["Location"]
    assert db_query("SELECT id FROM recurring_booking_rules") == []
    assert db_query("SELECT id FROM bookings WHERE weekly_rule_id=?",(rule_id,)) == []
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id IS NULL"))==1
    confirmation=client.get(response.headers["Location"]).get_data(as_text=True)
    assert "تم حذف الحجز الأسبوعي نهائيًا" in confirmation


@pytest.mark.parametrize("history_type",["booking_payment","deposit_transaction"])
def test_hard_delete_is_blocked_when_weekly_series_has_financial_history(client,history_type):
    login_as_admin(client)
    customer=add_client(name=f"حساب {history_type}")
    deposit=75 if history_type=="deposit_transaction" else 0
    post_form(client,"/admin/recurring-bookings/new",recurring_payload(customer,deposit=deposit))
    rule_id=db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    if history_type=="booking_payment":
        booking_id=db_query("SELECT id FROM bookings WHERE weekly_rule_id=? ORDER BY booking_date LIMIT 1",(rule_id,))[0]["id"]
        with stadium.app.app_context():
            db=stadium.get_db(); db.execute("INSERT INTO booking_payments (booking_id,amount) VALUES (?,10)",(booking_id,)); db.commit()
    with stadium.app.app_context():
        rules=stadium.get_recurring_booking_rules()
    assert rules[0]["can_hard_delete"] is False
    response=post_form(client,f"/admin/recurring-bookings/{rule_id}/delete")
    assert response.status_code==302
    assert len(db_query("SELECT id FROM recurring_booking_rules WHERE id=?",(rule_id,)))==1
    assert len(db_query("SELECT id FROM bookings WHERE weekly_rule_id=?",(rule_id,)))==52
    message=client.get(response.headers["Location"]).get_data(as_text=True)
    assert "مرتبطة بدفعات أو حركات عربون محفوظة" in message
    assert "حذفها مقيد بسجل مالي" in message


def test_weekly_delete_requires_login_and_csrf_and_handles_missing_rule(client):
    assert client.get("/admin/recurring-bookings/new").status_code==302
    assert client.post("/admin/recurring-bookings/999/delete").status_code==400
    login_as_admin(client)
    customer=add_client()
    post_form(client,"/admin/recurring-bookings/new",recurring_payload(customer,deposit=0))
    rule_id=db_query("SELECT id FROM recurring_booking_rules")[0]["id"]
    assert client.post(f"/admin/recurring-bookings/{rule_id}/delete",data={}).status_code==400
    assert len(db_query("SELECT id FROM recurring_booking_rules WHERE id=?",(rule_id,)))==1
    missing=post_form(client,"/admin/recurring-bookings/999/delete")
    assert missing.status_code==302
    assert "سبق حذفه" in client.get(missing.headers["Location"]).get_data(as_text=True)
