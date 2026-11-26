"""Run with:  python -m unittest -v test_doctorson"""
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

_tmp = tempfile.mkdtemp()
os.environ["DOCTORSON_DB"] = os.path.join(_tmp, "test.db")
os.environ["DOCTORSON_ADMIN_PASSWORD"] = "testpw"

import app as doctorson  # noqa: E402

TZ = doctorson.TZ


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def slot(i, weekday, start, end, hosp="Test Hospital", hid=1):
    return {"id": i, "doctor_id": 1, "hospital_id": hid, "weekday": weekday,
            "start_time": start, "end_time": end, "hospital_name": hosp,
            "hospital_city": "X", "hospital_address": "Y", "hospital_phone": "1"}


DOC = {"id": 1, "name": "Dr. T", "speciality": "Test", "phone": "", "email": "", "bio": ""}


class AvailabilityTests(unittest.TestCase):
    # 2026-11-23 is a Monday
    def test_available_inside_window(self):
        d = doctorson.build_doctor(DOC, [slot(1, 0, "09:00", "13:00")], at(2026, 11, 23, 10, 30))
        self.assertEqual(d["status"], "available")
        self.assertIn("1:00 PM", d["status_detail"])
        self.assertEqual(d["current_hospital"]["name"], "Test Hospital")

    def test_start_inclusive_end_exclusive(self):
        s = [slot(1, 0, "09:00", "13:00")]
        self.assertEqual(doctorson.build_doctor(DOC, s, at(2026, 11, 23, 9, 0))["status"], "available")
        self.assertNotEqual(doctorson.build_doctor(DOC, s, at(2026, 11, 23, 13, 0))["status"], "available")

    def test_upcoming_later_today(self):
        d = doctorson.build_doctor(DOC, [slot(1, 0, "14:00", "18:00")], at(2026, 11, 23, 8, 0))
        self.assertEqual(d["status"], "upcoming")
        self.assertEqual(d["next_hospital"]["name"], "Test Hospital")

    def test_off_shows_next_day(self):
        d = doctorson.build_doctor(DOC, [slot(1, 1, "09:00", "12:00")], at(2026, 11, 23, 20, 0))
        self.assertEqual(d["status"], "off")
        self.assertIn("tomorrow", d["status_detail"])

    def test_wraps_to_next_week(self):
        d = doctorson.build_doctor(DOC, [slot(1, 0, "09:00", "10:00")], at(2026, 11, 23, 11, 0))
        self.assertEqual(d["status"], "off")
        self.assertIn("Monday", d["status_detail"])

    def test_no_schedule(self):
        d = doctorson.build_doctor(DOC, [], at(2026, 11, 23, 11, 0))
        self.assertEqual(d["status_label"], "No schedule listed")
        self.assertIsNone(d["current_hospital"])

    def test_two_hospitals_same_day(self):
        s = [slot(1, 0, "08:00", "12:00", "A", hid=1), slot(2, 0, "15:00", "19:00", "B", hid=2)]
        d_morning = doctorson.build_doctor(DOC, s, at(2026, 11, 23, 9, 0))
        self.assertIn("A", d_morning["status_detail"])
        self.assertEqual(d_morning["current_hospital"]["name"], "A")
        d = doctorson.build_doctor(DOC, s, at(2026, 11, 23, 13, 0))
        self.assertEqual(d["status"], "upcoming")
        self.assertIn("B", d["status_detail"])

    def test_midnight_24_hour_end(self):
        s = [slot(1, 0, "20:00", "24:00", "Night ER")]
        d = doctorson.build_doctor(DOC, s, at(2026, 11, 23, 23, 59))
        self.assertEqual(d["status"], "available")
        self.assertIn("12:00 AM", d["status_detail"])


class PrescriptionPolicyTests(unittest.TestCase):
    def test_prescription_date_accepts_order_day_and_previous_day_only(self):
        order_day = at(2026, 11, 26, 10, 0).date()
        self.assertEqual(doctorson._prescription_date_check("2026-11-26", order_day), (True, "order_date"))
        self.assertEqual(doctorson._prescription_date_check("2026-11-25", order_day), (True, "previous_day"))
        self.assertEqual(doctorson._prescription_date_check("2026-11-24", order_day), (False, "too_old"))
        self.assertEqual(doctorson._prescription_date_check("2026-11-27", order_day), (False, "future_date"))
        self.assertEqual(doctorson._prescription_date_check("not-a-date", order_day), (False, "unreadable"))

    def test_doctor_name_normalisation_removes_title_and_punctuation(self):
        self.assertEqual(
            doctorson._normalise_doctor_name("Dr. Anika Perera"),
            doctorson._normalise_doctor_name("ANIKA-PERERA"),
        )


def count_slots():
    con = sqlite3.connect(doctorson.DB_PATH)
    try:
        return con.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
    finally:
        con.close()


class AppTests(unittest.TestCase):
    def setUp(self):
        doctorson.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
        doctorson._LOGIN_FAILS.clear()
        doctorson.init_db()
        self.c = doctorson.app.test_client()

    def _with_csrf(self, token="tok"):
        with self.c.session_transaction() as s:
            s["csrf"] = token
        return token

    def _login_admin(self, token="tok"):
        self._with_csrf(token)
        return self.c.post("/admin/login", data={"password": "testpw", "csrf": token})

    def test_home_and_api(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        data = self.c.get("/api/doctors").get_json()
        self.assertEqual(data["count"], 8)
        self.assertIn("hospitals", data["doctors"][0])
        self.assertIn("degree", data["doctors"][0])
        self.assertIn("whatsapp", data["doctors"][0])

    def test_degree_and_whatsapp_and_hospital_highlight_rendered(self):
        html = self.c.get("/doctor/1").get_data(as_text=True)
        self.assertIn("MBBS, MD (Cardiology)", html)
        self.assertIn("https://wa.me/94770000001", html)
        self.assertIn("Where and when Dr. Anika Perera works", html)
        # Dr. Fatima Hassan has no WhatsApp number listed
        html_no_wa = self.c.get("/doctor/7").get_data(as_text=True)
        self.assertNotIn("https://wa.me/", html_no_wa)

    def test_filters(self):
        self.assertEqual(self.c.get("/api/doctors?speciality=Cardiology").get_json()["count"], 1)
        self.assertEqual(self.c.get("/api/doctors?q=lakeside").get_json()["count"], 4)
        self.assertEqual(self.c.get("/api/doctors?q=zzzz").get_json()["count"], 0)
        self.assertEqual(self.c.get("/api/doctors?q=MBBS").get_json()["count"], 8)

    def test_hospital_plus_available_checks_current_hospital(self):
        # At Monday 10:00 AM, Dr. Samuel (id=4) works at Greenfield (id=2), NOT Lakeside (id=1).
        orig_now = doctorson.now_local
        doctorson.now_local = lambda: at(2026, 11, 23, 10, 0)
        try:
            res = self.c.get("/api/doctors?hospital=1&available=1").get_json()
            ids = [d["id"] for d in res["doctors"]]
            self.assertIn(1, ids)       # Dr. Anika is at Lakeside 09:00-13:00
            self.assertNotIn(4, ids)    # Dr. Samuel is at Greenfield 08:00-12:00, not Lakeside
        finally:
            doctorson.now_local = orig_now

    def test_sql_injection_attempt_is_harmless(self):
        r = self.c.get("/api/doctors?q=' OR 1=1 --")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 0)

    def test_doctor_page_and_404(self):
        self.assertEqual(self.c.get("/doctor/1").status_code, 200)
        self.assertEqual(self.c.get("/doctor/9999").status_code, 404)
        self.assertEqual(self.c.get("/api/doctors/9999").status_code, 404)

    def test_partial_and_static_js(self):
        self.assertIn("doctor", self.c.get("/partials/doctors").get_data(as_text=True))
        self.assertEqual(self.c.get("/static/app.js").status_code, 200)

    def test_sos_emergency_system(self):
        r = self.c.get("/sos")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Ambulance", html)
        self.assertIn("Call ER:", html)

        sos_json = self.c.get("/api/sos").get_json()
        self.assertEqual(sos_json["count"], 6)
        self.assertTrue(all(d["is_emergency"] for d in sos_json["doctors"]))
        self.assertEqual(self.c.get("/partials/sos").status_code, 200)

    def test_pharmacy_order_and_admin_tracking(self):
        self.assertEqual(self.c.get("/pharmacy").status_code, 200)
        meds = self.c.get("/api/pharmacy/medicines").get_json()
        self.assertGreaterEqual(meds["count"], 10)

        tok = self._with_csrf()
        # Add OTC medicine (id=1, Paracetamol 500mg) to cart
        r = self.c.post("/pharmacy/cart/add", data={"medicine_id": "1", "quantity": "2", "csrf": tok})
        self.assertEqual(r.status_code, 302)

        # Checkout
        r = self.c.post("/pharmacy/checkout", data={
            "customer_name": "Nimal Silva",
            "phone": "+94 77 111 2222",
            "whatsapp": "+94 77 111 2222",
            "address": "22 Temple Road",
            "city": "Colombo",
            "payment_method": "Cash on Delivery",
            "csrf": tok,
        })
        self.assertEqual(r.status_code, 302)
        order_url = r.headers["Location"]
        self.assertIn("/pharmacy/order/DO-", order_url)
        order_code = order_url.rsplit("/", 1)[-1]

        order_html = self.c.get(order_url).get_data(as_text=True)
        self.assertIn("Nimal Silva", order_html)
        self.assertIn("Paracetamol 500mg", order_html)

        # Verify stock decremented (120 -> 118)
        con = sqlite3.connect(doctorson.DB_PATH)
        stock_after = con.execute("SELECT stock FROM medicines WHERE id = 1").fetchone()[0]
        con.close()
        self.assertEqual(stock_after, 118)

        # Track page & JSON API
        self.assertIn(order_code, self.c.get(f"/pharmacy/track?code={order_code}").get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/pharmacy/orders/{order_code}").get_json()["status"], "Pending")

        # Admin cancels order -> stock restored to 120
        self._login_admin(tok)
        self.assertEqual(self.c.get("/admin/pharmacy").status_code, 200)
        self.c.post("/admin/pharmacy/orders/1/status", data={"status": "Cancelled", "csrf": tok})
        con = sqlite3.connect(doctorson.DB_PATH)
        stock_restored = con.execute("SELECT stock FROM medicines WHERE id = 1").fetchone()[0]
        con.close()
        self.assertEqual(stock_restored, 120)

    def test_ai_prescription_upload_grants_offer_only_for_enrolled_doctor_and_valid_date(self):
        original_key = doctorson.OPENAI_API_KEY
        original_dir = doctorson.PRESCRIPTION_DIR
        original_extract = doctorson._openai_prescription_extract
        doctorson.OPENAI_API_KEY = "test-api-key"
        doctorson.PRESCRIPTION_DIR = tempfile.mkdtemp()
        doctorson._openai_prescription_extract = lambda *_args: {
            "doctor_name": "Dr. Anika Perera",
            "prescription_date": doctorson.now_local().date().isoformat(),
            "medicines": [{"name": "Amoxicillin", "strength": "500mg", "directions_as_written": "As written"}],
            "readable": True,
            "notes": "Extraction completed",
        }
        try:
            con = sqlite3.connect(doctorson.DB_PATH)
            con.execute("UPDATE doctors SET pharmacy_discount_eligible=1 WHERE id=1")
            con.commit()
            con.close()

            tok = self._with_csrf()
            self.c.post("/pharmacy/cart/add", data={"medicine_id": "7", "quantity": "1", "csrf": tok})
            image_bytes = b"\x89PNG\r\n\x1a\nfake-png-data"
            analysed = self.c.post(
                "/pharmacy/prescription/analyze",
                data={
                    "csrf": tok,
                    "rx_ai_consent": "1",
                    "prescription_file": (io.BytesIO(image_bytes), "prescription.png"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(analysed.status_code, 200)
            result = analysed.get_json()
            self.assertTrue(result["discount_eligible"])

            placed = self.c.post(
                "/pharmacy/checkout",
                data={
                    "csrf": tok,
                    "rx_upload_token": result["token"],
                    "customer_name": "Test Customer",
                    "phone": "9876543210",
                    "address": "Test address",
                    "payment_method": "Cash on Delivery",
                    "prescription_file": (io.BytesIO(image_bytes), "prescription.png"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(placed.status_code, 302)
            order_code = placed.headers["Location"].rsplit("/", 1)[-1]
            order = self.c.get("/api/pharmacy/orders/" + order_code).get_json()
            self.assertEqual(order["discount_percent"], 15.0)
            self.assertEqual(order["discount_amount"], 21.75)
        finally:
            doctorson.OPENAI_API_KEY = original_key
            doctorson.PRESCRIPTION_DIR = original_dir
            doctorson._openai_prescription_extract = original_extract

    def test_admin_requires_login(self):
        r = self.c.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])

    def test_post_without_csrf_rejected(self):
        self.assertEqual(self.c.post("/admin/login", data={"password": "testpw"}).status_code, 400)

    def test_admin_flow_and_overnight_schedules(self):
        tok = self._with_csrf()
        r = self.c.post("/admin/login", data={"password": "wrong", "csrf": tok})
        self.assertEqual(r.status_code, 200)
        r = self.c.post("/admin/login", data={"password": "testpw", "csrf": tok})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/admin").status_code, 200)

        self.c.post("/admin/doctors", data={
            "name": "Dr. New",
            "degree": "MBBS, MD",
            "speciality": "Oncology",
            "whatsapp": "+94 77 999 8888",
            "is_emergency": "1",
            "csrf": tok,
        })
        res = self.c.get("/api/doctors?speciality=Oncology").get_json()
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["doctors"][0]["degree"], "MBBS, MD")

        # invalid schedule (end before start without overnight) is rejected
        before = count_slots()
        self.c.post("/admin/schedules", data={"doctor_id": "1", "hospital_id": "1", "weekday": "0",
                                              "start_time": "15:00", "end_time": "09:00", "csrf": tok})
        self.assertEqual(count_slots(), before)

        # overnight schedule splits cleanly into 2 slots
        self.c.post("/admin/schedules", data={
            "doctor_id": "1", "hospital_id": "1", "weekday": "0",
            "start_time": "22:00", "end_time": "06:00", "overnight": "1", "csrf": tok,
        })
        self.assertEqual(count_slots(), before + 2)

        # valid same-day schedule (3 days -> 3 slots)
        before2 = count_slots()
        self.c.post("/admin/schedules", data={"doctor_id": "1", "hospital_id": "1",
                                              "weekday": ["1", "2", "3"], "start_time": "09:00",
                                              "end_time": "10:00", "csrf": tok})
        self.assertEqual(count_slots(), before2 + 3)


if __name__ == "__main__":
    unittest.main()
