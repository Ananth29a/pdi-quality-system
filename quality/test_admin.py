from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import ChecklistItem, Gauge, GatePass, InspectorEntry, PDIChecklist, Review

User = get_user_model()


class AdminRuleTests(TestCase):
    """The admin must not become a back door around the model rules."""

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="x")  # acts as supervisor / clerk
        self.inspector = User.objects.create_user("insp", password="x")
        self.client.force_login(self.boss)
        today = timezone.localdate()
        self.good = Gauge.objects.create(gauge_id="VC-1", calibration_expiry=today + timedelta(days=30))
        self.expired = Gauge.objects.create(gauge_id="VC-OLD", calibration_expiry=today - timedelta(days=1))
        self.item = ChecklistItem.objects.create(
            section=4, step_number=Decimal("4.10"), parameter_name="Shaft",
            min_tolerance=Decimal("24.98"), max_tolerance=Decimal("25.05"))
        self.checklist = PDIChecklist.objects.create(asset_serial_number="LOT-1", inspector=self.inspector)

    def add(self, model, data):
        return self.client.post(reverse(f"admin:quality_{model}_add"), data)

    # --- inspector entry ---
    def test_entry_via_admin_is_validated_and_computed(self):
        r = self.add("inspectorentry", {"checklist": self.checklist.pk, "item": self.item.pk,
                                        "gauge": self.good.pk, "actual_value": "25.06"})
        self.assertEqual(r.status_code, 302)
        entry = InspectorEntry.objects.get()
        self.assertFalse(entry.is_checked)          # computed, not typed
        self.assertEqual(entry.entered_by, self.boss)  # stamped from the logged-in user

    def test_entry_with_expired_gauge_rejected_in_admin(self):
        r = self.add("inspectorentry", {"checklist": self.checklist.pk, "item": self.item.pk,
                                        "gauge": self.expired.pk, "actual_value": "25.00"})
        self.assertEqual(r.status_code, 200)         # form re-shown with an error
        self.assertEqual(InspectorEntry.objects.count(), 0)

    def test_entries_cannot_be_edited_or_deleted_in_admin(self):
        entry = InspectorEntry.objects.create(checklist=self.checklist, item=self.item, gauge=self.good,
                                              actual_value=Decimal("25.00"), entered_by=self.inspector)
        self.client.post(reverse("admin:quality_inspectorentry_change", args=[entry.pk]),
                         {"actual_value": "24.00"})
        entry.refresh_from_db()
        self.assertEqual(entry.actual_value, Decimal("25.0000"))
        r = self.client.post(reverse("admin:quality_inspectorentry_delete", args=[entry.pk]), {"post": "yes"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(InspectorEntry.objects.count(), 1)

    # --- review ---
    def _closed_checklist(self, value="25.00"):
        InspectorEntry.objects.create(checklist=self.checklist, item=self.item, gauge=self.good,
                                      actual_value=Decimal(value), entered_by=self.inspector)
        self.checklist.refresh_from_db()
        self.checklist.submit()

    def test_review_via_admin_works_for_another_users_checklist(self):
        self._closed_checklist()
        r = self.add("review", {"checklist": self.checklist.pk, "decision": "APPROVED",
                                "reason_code": "", "justification": ""})
        self.assertEqual(r.status_code, 302)
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "APPROVED")

    def test_review_of_own_checklist_blocked_in_admin(self):
        self.checklist.inspector = self.boss
        self.checklist.save()
        self._closed_checklist()
        r = self.add("review", {"checklist": self.checklist.pk, "decision": "APPROVED",
                                "reason_code": "", "justification": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Review.objects.count(), 0)

    # --- gate pass ---
    def test_gate_pass_blocked_in_admin_until_approved(self):
        self._closed_checklist()   # PASSED but not yet reviewed
        r = self.add("gatepass", {"checklist": self.checklist.pk, "customer_po": "PO-1", "vehicle_details": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(GatePass.objects.count(), 0)

    def test_gate_pass_issued_in_admin_after_approval(self):
        self._closed_checklist()
        self.add("review", {"checklist": self.checklist.pk, "decision": "APPROVED",
                            "reason_code": "", "justification": ""})
        r = self.add("gatepass", {"checklist": self.checklist.pk, "customer_po": "PO-1", "vehicle_details": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(GatePass.objects.get().issued_by, self.boss)

    # --- checklist submit action ---
    def test_submit_action(self):
        InspectorEntry.objects.create(checklist=self.checklist, item=self.item, gauge=self.good,
                                      actual_value=Decimal("25.00"), entered_by=self.inspector)
        self.client.post(reverse("admin:quality_pdichecklist_changelist"),
                         {"action": "submit_checklists", "_selected_action": [self.checklist.pk]})
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "PASSED")