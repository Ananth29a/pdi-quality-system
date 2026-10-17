from django.test import TestCase
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import ChecklistItem, Gauge, InspectorEntry, PDIChecklist


class PDITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("insp", password="x")
        today = timezone.localdate()
        self.good = Gauge.objects.create(gauge_id="VC-104", calibration_expiry=today + timedelta(days=30))
        self.expired = Gauge.objects.create(gauge_id="VC-OLD", calibration_expiry=today - timedelta(days=1))
        self.item1 = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.10"), parameter_name="Shaft diameter",
            min_tolerance=Decimal("24.98"), max_tolerance=Decimal("25.05"))
        self.item2 = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.20"), parameter_name="Thread pitch",
            min_tolerance=Decimal("1.00"), max_tolerance=Decimal("1.10"))
        self.checklist = PDIChecklist.objects.create(asset_serial_number="LOT-1", inspector=self.user)

    def enter(self, item, value, gauge=None, checklist=None):
        return InspectorEntry.objects.create(
            checklist=checklist or self.checklist, item=item, actual_value=Decimal(value),
            gauge=gauge or self.good, entered_by=self.user)

    # --- tolerance boundaries ---
    def test_upper_limit_passes(self):
        self.assertTrue(self.enter(self.item1, "25.05").is_checked)

    def test_lower_limit_passes(self):
        self.assertTrue(self.enter(self.item1, "24.98").is_checked)

    def test_above_upper_fails(self):
        self.assertFalse(self.enter(self.item1, "25.06").is_checked)

    def test_below_lower_fails(self):
        self.assertFalse(self.enter(self.item1, "24.97").is_checked)

    # --- gauge calibration ---
    def test_expired_gauge_blocked(self):
        with self.assertRaises(ValidationError):
            self.enter(self.item1, "25.00", gauge=self.expired)

    def test_gauge_expiring_today_is_valid(self):
        g = Gauge.objects.create(gauge_id="VC-TODAY", calibration_expiry=timezone.localdate())
        self.assertTrue(g.is_calibrated())

    def test_missing_gauge_blocked(self):
        with self.assertRaises(ValidationError):
            InspectorEntry.objects.create(checklist=self.checklist, item=self.item1,
                                          actual_value=Decimal("25.00"), entered_by=self.user)

    # --- step lock ---
    def test_cannot_skip_step(self):
        with self.assertRaises(ValidationError):
            self.enter(self.item2, "1.05")

    def test_duplicate_entry_blocked(self):
        self.enter(self.item1, "25.00")
        with self.assertRaises(ValidationError):
            self.enter(self.item1, "25.01")

    # --- CTQ freeze ---
    def test_ctq_failure_freezes_checklist(self):
        self.item1.is_ctq = True
        self.item1.save()
        self.enter(self.item1, "25.06")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "FAILED")
        with self.assertRaises(ValidationError):
            self.enter(self.item2, "1.05")

    def test_non_ctq_failure_allows_continuing(self):
        self.enter(self.item1, "25.06")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "IN_PROGRESS")
        self.assertTrue(self.enter(self.item2, "1.05").is_checked)

    # --- closed / read-only ---
    def test_closed_checklist_rejects_entries(self):
        self.enter(self.item1, "25.00")
        self.enter(self.item2, "1.05")
        self.checklist.submit()
        self.assertEqual(self.checklist.status, "PASSED")
        other = ChecklistItem.objects.create(
            section=5, step_number=Decimal("5.10"), parameter_name="X", max_tolerance=Decimal("1"))
        with self.assertRaises(ValidationError):
            self.enter(other, "0.5")

    def test_entry_cannot_be_edited_or_deleted(self):
        e = self.enter(self.item1, "25.00")
        e.actual_value = Decimal("25.01")
        with self.assertRaises(ValidationError):
            e.save()
        with self.assertRaises(ValidationError):
            e.delete()

    def test_submit_requires_all_items(self):
        self.enter(self.item1, "25.00")
        with self.assertRaises(ValidationError):
            self.checklist.submit()

    # --- visual checks ---
    def test_visual_item_uses_inspector_answer(self):
        v = ChecklistItem.objects.create(section=5, step_number=Decimal("5.10"),
                                         parameter_name="Free of burrs", check_type="VISUAL")
        ok = InspectorEntry.objects.create(checklist=self.checklist, item=v,
                                           visual_ok=True, entered_by=self.user)
        self.assertTrue(ok.is_checked)

    # --- template validation ---
    def test_min_greater_than_max_rejected(self):
        with self.assertRaises(ValidationError):
            ChecklistItem.objects.create(section=5, step_number=Decimal("5.20"), parameter_name="Bad",
                                         min_tolerance=Decimal("2"), max_tolerance=Decimal("1"))

    def test_numeric_without_limits_rejected(self):
        with self.assertRaises(ValidationError):
            ChecklistItem.objects.create(section=5, step_number=Decimal("5.30"), parameter_name="Bad")
