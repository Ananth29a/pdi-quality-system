from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import ChecklistItem, Gauge, GatePass, InspectorEntry, PDIChecklist, Review

User = get_user_model()


class GatePassTests(TestCase):
    def setUp(self):
        self.inspector = User.objects.create_user("insp", password="x")
        self.supervisor = self._user("sup", "can_review_pdi")
        self.clerk = self._user("clerk", "can_issue_gate_pass")
        self.gauge = Gauge.objects.create(
            gauge_id="VC-1", calibration_expiry=timezone.localdate() + timedelta(days=30))
        self.item1 = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.10"), parameter_name="Shaft diameter",
            min_tolerance=Decimal("24.98"), max_tolerance=Decimal("25.05"))
        self.item2 = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.20"), parameter_name="Thread pitch", is_ctq=True,
            min_tolerance=Decimal("1.00"), max_tolerance=Decimal("1.10"))
        self.checklist = PDIChecklist.objects.create(
            asset_serial_number="LOT-1", inspector=self.inspector)

    def _user(self, name, perm):
        user = User.objects.create_user(name, password="x")
        user.user_permissions.add(Permission.objects.get(codename=perm))
        return User.objects.get(pk=user.pk)

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
        Review.objects.create(checklist=self.checklist, decision=decision,
                              reviewed_by=self.supervisor, **kw)

    def gate_pass(self, **kw):
        kw.setdefault("issued_by", self.clerk)
        kw.setdefault("customer_po", "PO-1001")
        return GatePass.objects.create(checklist=self.checklist, **kw)

    # --- allowed ---
    def test_gate_pass_for_approved_lot(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        gp = self.gate_pass()
        self.assertTrue(gp.number.startswith("GP-"))
        self.assertFalse(gp.released_on_concession)

    def test_gate_pass_for_concession_lot_is_flagged(self):
        self.fill("25.06", "1.05")
        self.review("CONCESSION", reason_code="FUNCTIONAL_OK", justification="Fit is unaffected.")
        gp = self.gate_pass()
        self.assertTrue(gp.released_on_concession)

    # --- blocked ---
    def test_blocked_when_pdi_in_progress(self):
        with self.assertRaisesMessage(ValidationError, "not completed"):
            self.gate_pass()

    def test_blocked_when_awaiting_supervisor(self):
        self.fill("25.00", "1.05")  # PASSED, no review yet
        with self.assertRaisesMessage(ValidationError, "awaiting supervisor approval"):
            self.gate_pass()

    def test_blocked_when_failed_and_unreviewed(self):
        self.fill("25.00", "1.20")  # CTQ failure freezes the checklist
        with self.assertRaisesMessage(ValidationError, "awaiting a supervisor decision"):
            self.gate_pass()

    def test_blocked_when_quarantined(self):
        self.fill("25.00", "1.20")
        self.review("QUARANTINE")
        with self.assertRaisesMessage(ValidationError, "quarantined"):
            self.gate_pass()

    # --- who / how often ---
    def test_user_without_permission_blocked(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        plain = User.objects.create_user("plain", password="x")
        with self.assertRaises(ValidationError):
            self.gate_pass(issued_by=plain)

    def test_only_one_gate_pass_per_lot(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        self.gate_pass()
        with self.assertRaises(ValidationError):
            self.gate_pass()

    def test_customer_po_required(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        with self.assertRaises(ValidationError):
            self.gate_pass(customer_po="")

    def test_gate_pass_is_permanent(self):
        self.fill("25.00", "1.05")
        self.review("APPROVED")
        gp = self.gate_pass()
        gp.customer_po = "CHANGED"
        with self.assertRaises(ValidationError):
            gp.save()
        with self.assertRaises(ValidationError):
            gp.delete()