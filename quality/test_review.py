from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import ChecklistItem, Gauge, InspectorEntry, PDIChecklist, Review

User = get_user_model()


class ReviewTests(TestCase):
    def setUp(self):
        self.inspector = User.objects.create_user("insp", password="x")
        self.supervisor = self._supervisor("sup")
        self.gauge = Gauge.objects.create(
            gauge_id="VC-1", calibration_expiry=timezone.localdate() + timedelta(days=30))
        # 4.10 is a normal item, 4.20 is CTQ
        self.item1 = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.10"), parameter_name="Shaft diameter",
            min_tolerance=Decimal("24.98"), max_tolerance=Decimal("25.05"))
        self.item2 = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.20"), parameter_name="Thread pitch", is_ctq=True,
            min_tolerance=Decimal("1.00"), max_tolerance=Decimal("1.10"))
        self.checklist = PDIChecklist.objects.create(
            asset_serial_number="LOT-1", inspector=self.inspector)

    def _supervisor(self, name):
        user = User.objects.create_user(name, password="x")
        user.user_permissions.add(Permission.objects.get(codename="can_review_pdi"))
        return User.objects.get(pk=user.pk)  # fresh instance, no stale permission cache

    def fill(self, v1, v2):
        for item, val in ((self.item1, v1), (self.item2, v2)):
            if self.checklist.status != "IN_PROGRESS":
                break
            InspectorEntry.objects.create(
                checklist=self.checklist, item=item, actual_value=Decimal(val),
                gauge=self.gauge, entered_by=self.inspector)
        self.checklist.refresh_from_db()
        if self.checklist.status == "IN_PROGRESS":
            self.checklist.submit()

    def review(self, decision, **kw):
        return Review.objects.create(
            checklist=self.checklist, decision=decision,
            reviewed_by=kw.pop("by", self.supervisor), **kw)

    # --- approve ---
    def test_approve_clean_checklist(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "APPROVED")

    def test_cannot_approve_failing_checklist(self):
        self.fill("25.06", "1.05")
        with self.assertRaises(ValidationError):
            self.review("APPROVED")

    # --- concession ---
    def test_concession_on_non_ctq_failure(self):
        self.fill("25.06", "1.05")
        self.review("CONCESSION", reason_code="FUNCTIONAL_OK",
                    justification="Within functional fit per drawing note 3.")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "CONCESSION")

    def test_concession_needs_justification(self):
        self.fill("25.06", "1.05")
        with self.assertRaises(ValidationError):
            self.review("CONCESSION", reason_code="FUNCTIONAL_OK", justification="   ")

    def test_concession_needs_reason_code(self):
        self.fill("25.06", "1.05")
        with self.assertRaises(ValidationError):
            self.review("CONCESSION", justification="Looks fine.")

    def test_concession_blocked_for_ctq_failure(self):
        self.fill("25.00", "1.20")  # CTQ out of tolerance
        with self.assertRaises(ValidationError):
            self.review("CONCESSION", reason_code="OTHER", justification="Please.")

    def test_concession_with_nothing_to_concede(self):
        self.fill("25.00", "1.05")
        with self.assertRaises(ValidationError):
            self.review("CONCESSION", reason_code="OTHER", justification="n/a")

    # --- quarantine ---
    def test_quarantine_ctq_frozen_checklist(self):
        self.fill("25.00", "1.20")
        self.review("QUARANTINE")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "QUARANTINED")

    def test_quarantine_non_ctq_failure(self):
        self.fill("25.06", "1.05")
        self.review("QUARANTINE")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "QUARANTINED")

    def test_cannot_approve_incomplete_frozen_checklist(self):
        self.item1.is_ctq = True
        self.item1.save()
        self.fill("25.06", "1.05")  # CTQ fails on first item -> frozen, only 1 of 2 entries
        with self.assertRaises(ValidationError):
            self.review("APPROVED")

    # --- who / when ---
    def test_cannot_review_in_progress_checklist(self):
        with self.assertRaises(ValidationError):
            self.review("QUARANTINE")

    def test_user_without_permission_cannot_review(self):
        self.fill("25.00", "1.05")
        plain = User.objects.create_user("plain", password="x")
        with self.assertRaises(ValidationError):
            self.review("APPROVED", by=plain)

    def test_inspector_cannot_review_own_checklist(self):
        self.fill("25.00", "1.05")
        inspector_sup = self._supervisor("insp2")
        self.checklist.inspector = inspector_sup
        self.checklist.save()
        with self.assertRaises(ValidationError):
            self.review("APPROVED", by=inspector_sup)

    def test_only_one_review_per_checklist(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        with self.assertRaises(ValidationError):
            self.review("QUARANTINE")

    def test_review_is_permanent(self):
        self.fill("25.00", "1.05")
        r = self.review("APPROVED")
        r.decision = "QUARANTINE"
        with self.assertRaises(ValidationError):
            r.save()
        with self.assertRaises(ValidationError):
            r.delete()