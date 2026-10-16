from datetime import date, datetime

import pytest
from conftest import (
    add_client,
    booking_payload,
    db_query,
    insert_booking,
    login_as_admin,
    post_form,
)

import app as stadium


def test_booking_form_renders(client):
    login_as_admin(client); assert client.get("/admin/bookings/new").status_code == 200


@pytest.mark.parametrize("field,value", [("booking_date",""),("booking_date","31/12/2025"),("start_time","25:00"),("start_time","10:05"),("client_id",""),("client_id","-1"),("hourly_rate","bad"),("hourly_rate","-1"),("duration_hours","0"),("duration_hours","abc"),("initial_payment","-1")])
def test_invalid_booking_inputs_are_rejected(client, field, value):
    login_as_admin(client); customer = add_client(); payload = booking_payload(customer); payload[field] = value
    assert post_form(client,"/admin/bookings/new",payload).status_code == 400
    assert db_query("SELECT * FROM bookings") == []


@pytest.mark.parametrize("minute",["01","15","30","45","59"])
def test_booking_start_must_be_on_hour(client,minute):
    login_as_admin(client); customer=add_client()
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,start_time=f"14:{minute}")).status_code == 400


def test_initial_payment_create_conflict_and_adjacent_slot(client):
    login_as_admin(client); customer=add_client()
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,booking_date="2025-01-08",duration_hours=2,initial_payment=40)).status_code == 302
    assert db_query("SELECT amount FROM booking_payments")[0]["amount"] == 40
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,booking_date="2025-01-08",start_time="15:00")).status_code == 409
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,booking_date="2025-01-08",start_time="16:00")).status_code == 302


@pytest.mark.parametrize("payment",["101","-1","not-money"])
def test_initial_payment_cannot_exceed_total(client,payment):
    login_as_admin(client); customer=add_client()
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,initial_payment=payment)).status_code == 400
    assert db_query("SELECT * FROM bookings") == []


@pytest.mark.parametrize("field", ["hourly_rate", "initial_payment"])
def test_booking_rejects_financial_values_above_safe_limit(client, field):
    login_as_admin(client); customer=add_client()
    payload=booking_payload(customer)
    payload[field]=str(stadium.MAX_FINANCIAL_AMOUNT + 1)
    assert post_form(client,"/admin/bookings/new",payload).status_code == 400
    assert db_query("SELECT * FROM bookings") == []


def test_booking_wraps_midnight_and_edit_cannot_reduce_below_paid(client):
    login_as_admin(client); customer=add_client()
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,booking_date="2025-01-09",start_time="23:00",duration_hours=2,initial_payment=80)).status_code == 302
    row=db_query("SELECT * FROM bookings")[0]; assert row["end_time"]=="01:00"
    assert post_form(client,f"/admin/bookings/{row['id']}/edit",booking_payload(customer,booking_date="2025-01-09",hourly_rate=50)).status_code == 400
    assert post_form(client,f"/admin/bookings/{row['id']}/edit",booking_payload(customer,booking_date="2025-01-09",hourly_rate=120)).status_code == 302
    assert db_query("SELECT total FROM bookings")[0]["total"]==120


def test_cancel_retains_record_and_frees_time(client):
    login_as_admin(client); customer=add_client()
    post_form(client,"/admin/bookings/new",booking_payload(customer,booking_date="2025-01-10")); booking_id=db_query("SELECT id FROM bookings")[0]["id"]
    assert post_form(client,f"/admin/bookings/{booking_id}/cancel").status_code == 302
    assert db_query("SELECT status FROM bookings WHERE id=?",(booking_id,))[0]["status"]=="cancelled"
    assert post_form(client,"/admin/bookings/new",booking_payload(customer,booking_date="2025-01-10")).status_code==302


def test_cancel_missing_or_cancelled_and_payment_routes_are_safe(client):
    login_as_admin(client); assert post_form(client,"/admin/bookings/9999/cancel").status_code==302
    customer=add_client(); booking_id=insert_booking(customer,status="cancelled")
    assert post_form(client,f"/admin/bookings/{booking_id}/cancel").status_code==302
    assert post_form(client,f"/admin/bookings/{booking_id}/payments",{"amount":"10"}).status_code==409
    assert client.get("/admin/bookings/9999/payments").status_code==302


@pytest.mark.parametrize("amount",["0","-5","abc","","2.5"])
def test_payment_rejects_invalid_amount(client,amount):
    login_as_admin(client); customer=add_client(); booking_id=insert_booking(customer)
    assert post_form(client,f"/admin/bookings/{booking_id}/payments",{"amount":amount}).status_code==400
    assert db_query("SELECT * FROM booking_payments")==[]


def test_payment_rejects_extremely_large_amount_without_sqlite_overflow(client):
    login_as_admin(client); customer=add_client(); booking_id=insert_booking(customer)
    for amount in (str(stadium.MAX_FINANCIAL_AMOUNT + 1), "9" * 5000):
        response=post_form(client,f"/admin/bookings/{booking_id}/payments",{"amount":amount})
        assert response.status_code==400
    assert db_query("SELECT * FROM booking_payments")==[]


def test_payment_partial_full_overpayment_and_local_timestamp(client):
    login_as_admin(client); customer=add_client(); booking_id=insert_booking(customer,booking_date="2025-01-21")
    assert post_form(client,f"/admin/bookings/{booking_id}/payments",{"amount":"60"}).status_code==302
    assert post_form(client,f"/admin/bookings/{booking_id}/payments",{"amount":"40"}).status_code==302
    assert post_form(client,f"/admin/bookings/{booking_id}/payments",{"amount":"1"}).status_code==409
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("UPDATE booking_payments SET paid_at='2025-01-01 22:30:00' WHERE booking_id=?",(booking_id,)); db.commit()
        detail=stadium.get_booking_payment_details(booking_id)
    assert detail["payment_status"]=="مدفوع بالكامل" and detail["remaining"]==0
    html=client.get(f"/admin/bookings/{booking_id}/payments").get_data(as_text=True)
    assert "2025-01-02 01:30" in html and "2025-01-01 22:30:00" not in html


def test_report_summary_excludes_cancelled_and_calculates_amounts(client):
    customer=add_client(); active=insert_booking(customer,booking_date="2025-01-14",hourly_rate=120,duration=2,total=240)
    insert_booking(customer,booking_date="2025-01-14",start_time="17:00",status="cancelled",total=100)
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("INSERT INTO booking_payments (booking_id,amount) VALUES (?,90)",(active,)); db.commit()
        summary=stadium.get_booking_report_summary("2025-01-14","2025-01-14")
    assert (summary["booking_count"],summary["total_amount"],summary["total_paid"],summary["total_remaining"],summary["booked_hours"])==(1,240,90,150,2)


def test_reports_keep_after_midnight_booking_on_stored_business_date(client):
    customer=add_client(); insert_booking(customer,booking_date="2025-01-15",start_time="02:00")
    with stadium.app.app_context():
        prior=stadium.get_booking_report_summary("2025-01-14","2025-01-14")
        booked=stadium.get_booking_report_summary("2025-01-15","2025-01-15")
    assert prior["booking_count"]==0 and booked["booking_count"]==1


def make_rule(customer):
    with stadium.app.app_context():
        db=stadium.get_db(); cursor=db.execute("INSERT INTO recurring_booking_rules (client_id,weekday,start_time,duration_hours,hourly_rate,deposit,active) VALUES (?,0,'14:00',1,100,100,0)",(customer,)); db.commit(); return cursor.lastrowid


def test_deposit_reports_period_activity_and_cumulative_local_balance(client):
    customer=add_client(); rule=make_rule(customer)
    with stadium.app.app_context():
        db=stadium.get_db()
        db.execute("INSERT INTO recurring_deposit_transactions (rule_id,transaction_type,amount,created_at) VALUES (?,'collect',100,'2025-01-10 12:00:00')",(rule,))
        db.execute("INSERT INTO recurring_deposit_transactions (rule_id,transaction_type,amount,created_at) VALUES (?,'refund',100,'2025-02-10 12:00:00')",(rule,))
        db.execute("INSERT INTO recurring_deposit_transactions (rule_id,transaction_type,amount,created_at) VALUES (?,'collect',200,'2025-01-31 22:30:00')",(rule,)); db.commit()
        jan=stadium.get_booking_report_summary("2025-01-01","2025-01-31"); feb=stadium.get_booking_report_summary("2025-02-01","2025-02-28")
        local_feb1=stadium.get_booking_report_summary("2025-02-01","2025-02-01")
    assert (jan["deposit_collected"],jan["deposit_refunded"],jan["deposit_held"])==(100,0,100)
    assert (feb["deposit_collected"],feb["deposit_refunded"],feb["deposit_held"])==(200,100,200)
    assert (local_feb1["deposit_collected"],local_feb1["deposit_held"])==(200,300)


def test_public_policy_blocks_schedule_until_explicit_consent_and_policy_version(client,monkeypatch):
    monkeypatch.setattr(stadium,"app_now",lambda:datetime(2025,1,20,12,0))
    customer=add_client(name="عميل عام",phone="0791234567",notes="ملاحظة خاصة")
    insert_booking(customer,booking_date="2025-01-20",start_time="14:00",notes="سر")
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("UPDATE app_settings SET setting_value='سياسة أ' WHERE setting_key='venue_policy'"); db.commit()
    blocked=client.get("/public-schedule"); assert "booking-hours-grid" not in blocked.get_data(as_text=True)
    assert post_form(client,"/public-schedule",{"accept_policy":"true"}).status_code==400
    assert post_form(client,"/public-schedule",{"accept_policy":"yes"}).status_code==302
    body=client.get("/public-schedule?date=2025-01-20").get_data(as_text=True)
    assert "booking-hours-grid" in body and "ملاحظة خاصة" not in body and "سر" not in body
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("UPDATE app_settings SET setting_value='سياسة ب' WHERE setting_key='venue_policy'"); db.commit()
    assert "booking-hours-grid" not in client.get("/public-schedule").get_data(as_text=True)


def test_empty_policy_hides_slots_and_bad_date_is_safely_clamped(client):
    assert "لم يضف المسؤول سياسة الملعب بعد" in client.get("/public-schedule").get_data(as_text=True)
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("UPDATE app_settings SET setting_value='سياسة' WHERE setting_key='venue_policy'"); db.commit()
    client.get("/public-schedule"); post_form(client,"/public-schedule",{"accept_policy":"yes"})
    assert client.get("/public-schedule?date=1900-01-01").status_code==200


def test_public_slot_state_and_data_privacy(client):
    customer=add_client(name="اسم عام",phone="0791234567",notes="بيانات خاصة")
    insert_booking(customer,booking_date="2025-02-01",start_time="14:00",duration=2,total=200,notes="سر")
    with stadium.app.app_context(): slots=stadium.build_public_slots(date(2025,2,1),stadium.get_app_settings())
    assert slots[4]["state"]==slots[5]["state"]=="booked"
    assert slots[4]["client_names"]==["اسم عام"]
    assert not ({"phone","notes","total"}&set(slots[4]))


def test_booking_slots_show_deposit_state(client):
    customer=add_client(); booking_id=insert_booking(customer,booking_date="2025-02-02",start_time="12:00",duration=2,total=200)
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("INSERT INTO booking_payments (booking_id,amount) VALUES (?,20)",(booking_id,)); db.commit()
        slots,count,_=stadium.build_booking_slots(date(2025,2,2),stadium.get_app_settings())
    assert count==24 and slots[2]["booking"]["card_state"]==slots[3]["booking"]["card_state"]=="deposit"


def test_public_schedule_requires_separate_session_consent(client):
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("UPDATE app_settings SET setting_value='سياسة' WHERE setting_key='venue_policy'"); db.commit()
    client.get("/public-schedule"); post_form(client,"/public-schedule",{"accept_policy":"yes"})
    other=stadium.app.test_client()
    assert "booking-hours-grid" not in other.get("/public-schedule").get_data(as_text=True)
