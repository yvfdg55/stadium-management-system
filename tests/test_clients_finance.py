from datetime import datetime

import pytest
import app as stadium
from conftest import add_client, db_query, insert_booking, login_as_admin, post_form, booking_payload, recurring_payload


def test_admin_pages_render(client):
    login_as_admin(client)
    for path in ["/admin","/admin/account","/admin/settings","/admin/reports","/admin/booking","/admin/booking/hours","/admin/bookings/new","/admin/recurring-bookings/new","/admin/expenses","/admin/income","/admin/clients","/admin/clients/new","/admin/expenses/new","/admin/income/new"]:
        assert client.get(path).status_code in (200,302)


def test_clients_create_search_edit_delete_and_required_fields(client):
    login_as_admin(client)
    for name,phone in [("","0790"),("عميل","")]:
        assert post_form(client,"/admin/clients/new",{"name":name,"phone":phone,"notes":""}).status_code==200
    assert db_query("SELECT * FROM clients")==[]
    assert post_form(client,"/admin/clients/new",{"name":" سارة ","phone":" 0791234567 ","notes":"ملاحظة"}).status_code==302
    customer=db_query("SELECT * FROM clients")[0]; assert customer["name"]=="سارة" and customer["phone"]=="0791234567"
    assert "سارة" in client.get("/admin/clients?q=سارة").get_data(as_text=True)
    assert post_form(client,f"/admin/clients/{customer['id']}/edit",{"name":"سارة أحمد","phone":"0792","notes":"محدث"}).status_code==302
    assert client.get(f"/admin/clients/{customer['id']}").status_code==200
    assert post_form(client,f"/admin/clients/{customer['id']}/delete").status_code==302
    assert db_query("SELECT * FROM clients")==[]


def test_client_linked_to_booking_cannot_be_deleted_and_missing_routes_are_safe(client):
    login_as_admin(client); customer=add_client(); insert_booking(customer)
    assert post_form(client,f"/admin/clients/{customer}/delete").status_code==302
    assert db_query("SELECT id FROM clients WHERE id=?",(customer,))
    assert client.get("/admin/clients/9999").status_code==302
    assert client.get("/admin/clients/9999/edit").status_code==302


def test_client_detail_totals_and_deposit_balance(client):
    login_as_admin(client); customer=add_client(name="عميل التفاصيل"); booking=insert_booking(customer,total=200,duration=2)
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("INSERT INTO booking_payments (booking_id,amount) VALUES (?,70)",(booking,))
        rule=db.execute("INSERT INTO recurring_booking_rules (client_id,weekday,start_time,duration_hours,hourly_rate,deposit,active) VALUES (?,0,'14:00',1,100,50,0)",(customer,)).lastrowid
        db.execute("INSERT INTO recurring_deposit_transactions (rule_id,transaction_type,amount) VALUES (?,'collect',50)",(rule,)); db.execute("INSERT INTO recurring_deposit_transactions (rule_id,transaction_type,amount) VALUES (?,'refund',10)",(rule,)); db.commit()
    body=client.get(f"/admin/clients/{customer}").get_data(as_text=True)
    assert "عميل التفاصيل" in body and "200" in body and "70" in body and "130" in body and "40" in body


def finance_payload(amount="100",category="صيانة",date_field="expense_date",**changes):
    value={"amount":amount,"category":category,"custom_category":"","description":"وصف","payment_method":"cash",date_field:"2025-01-06","notes":"ملاحظات"}; value.update({k:str(v) for k,v in changes.items()}); return value


@pytest.mark.parametrize("base,date_field,table",[("/admin/expenses/new","expense_date","expenses"),("/admin/income/new","income_date","other_income")])
def test_financial_crud_and_search(base,date_field,table,client):
    login_as_admin(client)
    assert post_form(client,base,finance_payload("250","أخرى",date_field,custom_category="تجهيز",payment_method="transfer")).status_code==302
    row=db_query(f"SELECT * FROM {table}")[0]; assert row["amount"]==250 and row["category"]=="تجهيز" and row["payment_method"]=="transfer"
    list_path="/admin/expenses" if table=="expenses" else "/admin/income"
    assert client.get(list_path+"?q=تجهيز&start_date=2025-01-01&end_date=2025-01-31").status_code==200
    assert post_form(client,f"{list_path}/{row['id']}/edit",finance_payload("300","نظافة",date_field)).status_code==302
    assert db_query(f"SELECT amount,category FROM {table}")[0]=={"amount":300,"category":"نظافة"}
    assert post_form(client,f"{list_path}/{row['id']}/delete").status_code==302 and db_query(f"SELECT * FROM {table}")==[]


@pytest.mark.parametrize("amount,category,method,day",[("0","صيانة","cash","2025-01-06"),("-1","صيانة","cash","2025-01-06"),("bad","صيانة","cash","2025-01-06"),("10","","cash","2025-01-06"),("10","أخرى","cash","2025-01-06"),("10","صيانة","crypto","2025-01-06"),("10","صيانة","cash","bad")])
def test_financial_validation_rejects_bad_input(client,amount,category,method,day):
    login_as_admin(client)
    assert post_form(client,"/admin/expenses/new",finance_payload(amount,category,"expense_date",payment_method=method,expense_date=day)).status_code==200
    assert db_query("SELECT * FROM expenses")==[]


def test_finance_summary_totals(client):
    customer=add_client(); booking=insert_booking(customer,booking_date="2025-01-06",hourly_rate=100,duration=2,total=200)
    with stadium.app.app_context():
        db=stadium.get_db(); db.execute("INSERT INTO booking_payments (booking_id,amount) VALUES (?,80)",(booking,)); db.execute("INSERT INTO other_income (amount,category,payment_method,income_date) VALUES (50,'بيع','cash','2025-01-06')"); db.execute("INSERT INTO expenses (amount,category,payment_method,expense_date) VALUES (30,'كهرباء','cash','2025-01-06')"); db.commit()
        summary=stadium.get_finance_summary("2025-01-06","2025-01-06")
    assert summary=={"booking_income":80,"other_income":50,"total_income":130,"expenses":30,"net_income":100}


def test_custom_reports_validate_period(client):
    login_as_admin(client)
    for query in ["?start_date=2025-01-01","?start_date=bad&end_date=2025-01-01","?start_date=2025-02-01&end_date=2025-01-01"]:
        assert client.get("/admin/reports"+query).status_code==400
    assert client.get("/admin/reports?start_date=2025-01-01&end_date=2025-01-31").status_code==200


def settings_payload(start="10:00",end="22:00"):
    return {"venue_name":"الملعب","venue_phone":"","venue_address":"","venue_policy":"سياسة","hourly_rate":"100","work_start":start,"work_end":end,"booking_duration":"1","primary_color":"#237447","secondary_color":"#d64b27"}


def test_settings_reject_window_that_hides_active_booking_or_rule(client,monkeypatch):
    login_as_admin(client); customer=add_client(); insert_booking(customer,booking_date="2025-01-06",start_time="09:00")
    assert post_form(client,"/admin/settings",settings_payload()).status_code==400
    assert db_query("SELECT setting_value FROM app_settings WHERE setting_key='work_start'")[0]["setting_value"]=="10:00"
    # Keep the normal window but update other settings despite an old boundary booking.
    assert post_form(client,"/admin/settings",settings_payload("10:00","10:00")).status_code==302


def test_settings_reject_window_excluding_active_weekly_rule(client,monkeypatch):
    monkeypatch.setattr(stadium,"app_now",lambda:datetime(2025,1,6,12,0)); login_as_admin(client); customer=add_client()
    post_form(client,"/admin/recurring-bookings/new",recurring_payload(customer,start_time="14:00"))
    assert post_form(client,"/admin/settings",settings_payload("15:00","22:00")).status_code==400


def test_dashboard_shows_current_and_upcoming(client,monkeypatch):
    monkeypatch.setattr(stadium,"app_now",lambda:datetime(2025,1,6,15,0)); login_as_admin(client); customer=add_client(name="حجز اليوم")
    insert_booking(customer,booking_date="2025-01-06",start_time="14:00",duration=2); insert_booking(customer,booking_date="2025-01-06",start_time="18:00")
    response=client.get("/admin"); assert response.status_code==200 and "حجز اليوم" in response.get_data(as_text=True)


def test_account_validation_duplicate_and_logout(client):
    login_as_admin(client)
    invalid=[{"username":"","current_password":"QA-only-password-2026-do-not-reuse","new_password":"","confirm_password":""},{"username":"qa-admin","current_password":"wrong","new_password":"","confirm_password":""},{"username":"qa-admin","current_password":"QA-only-password-2026-do-not-reuse","new_password":"short","confirm_password":"short"},{"username":"qa-admin","current_password":"QA-only-password-2026-do-not-reuse","new_password":"long-enough-password","confirm_password":"different-password"}]
    for payload in invalid: assert post_form(client,"/admin/account",payload).status_code==200
    assert post_form(client,"/logout").status_code==302 and client.get("/admin").status_code==302


def test_missing_financial_records_are_handled(client):
    login_as_admin(client)
    for path in ["/admin/expenses/9999/edit","/admin/income/9999/edit"]: assert client.get(path).status_code==302
    for path in ["/admin/expenses/9999/delete","/admin/income/9999/delete"]: assert post_form(client,path).status_code==302
