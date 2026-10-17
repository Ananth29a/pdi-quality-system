from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


class Gauge(models.Model):
    """Measuring tool from the calibration register (BRD 2.3)."""
    gauge_id = models.CharField(max_length=50, unique=True)  # e.g. VC-104
    description = models.CharField(max_length=255, blank=True)
    calibration_expiry = models.DateField()

    def is_calibrated(self, today=None):
        """Valid through the expiry date; invalid the day after."""
        today = today or timezone.localdate()
        return self.calibration_expiry >= today

    def __str__(self):
        return self.gauge_id


class PDIChecklist(models.Model):
    """One inspection run for one lot / serial number."""
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'In Progress'),
        ('PASSED', 'Passed (all in tolerance)'),
        ('FAILED', 'Failed (needs supervisor decision)'),
        # set by a supervisor Review (BRD Section 6)
        ('APPROVED', 'Approved'),
        ('CONCESSION', 'Released on Concession'),
        ('QUARANTINED', 'Quarantined (Rejection Holding Area)'),
    ]
    # Not unique: a reworked lot must be inspectable again.
    asset_serial_number = models.CharField(max_length=100)
    inspector = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def submit(self):
        """Inspector finishes the checklist. All items must have an entry."""
        if self.status != 'IN_PROGRESS':
            raise ValidationError("Checklist is already closed.")
        if self.entries.count() < ChecklistItem.objects.count():
            raise ValidationError("All checklist items must be completed before submitting.")
        has_fail = self.entries.filter(is_checked=False).exists()
        self.status = 'FAILED' if has_fail else 'PASSED'
        self.save(update_fields=['status', 'updated_at'])

    def __str__(self):
        return f"PDI #{self.id} - {self.asset_serial_number}"


class ChecklistItem(models.Model):
    """Template item: what to measure and its limits."""
    SECTION_CHOICES = [
        (4, 'Section 4: PDI Template Items'),
        (5, 'Section 5: Inspector Workflow Items'),
    ]
    NUMERIC, VISUAL = 'NUMERIC', 'VISUAL'
    CHECK_TYPES = [(NUMERIC, 'Numeric'), (VISUAL, 'Visual pass/fail')]

    section = models.IntegerField(choices=SECTION_CHOICES)
    step_number = models.DecimalField(max_digits=4, decimal_places=2)
    parameter_name = models.CharField(max_length=255)
    check_type = models.CharField(max_length=10, choices=CHECK_TYPES, default=NUMERIC)
    is_ctq = models.BooleanField(default=False, verbose_name="Critical to Quality (CTQ)")
    min_tolerance = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    max_tolerance = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    unit_of_measure = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ['section', 'step_number']
        unique_together = ['section', 'step_number']

    def clean(self):
        lo, hi = self.min_tolerance, self.max_tolerance
        if self.check_type == self.NUMERIC:
            if lo is None and hi is None:
                raise ValidationError("Numeric items need at least one limit.")
            if lo is not None and hi is not None and lo > hi:
                raise ValidationError("min_tolerance cannot be greater than max_tolerance.")
        elif lo is not None or hi is not None:
            raise ValidationError("Visual items cannot have numeric limits.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.step_number} {'[CTQ] ' if self.is_ctq else ''}- {self.parameter_name}"


class InspectorEntry(models.Model):
    """One recorded measurement. Write-once: never editable or deletable."""
    checklist = models.ForeignKey(PDIChecklist, on_delete=models.PROTECT, related_name="entries")
    item = models.ForeignKey(ChecklistItem, on_delete=models.PROTECT)
    gauge = models.ForeignKey(Gauge, on_delete=models.PROTECT, null=True, blank=True)
    actual_value = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    visual_ok = models.BooleanField(null=True, blank=True)  # inspector's Yes/No for VISUAL items
    is_checked = models.BooleanField(default=False)          # computed result, never user input
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['checklist', 'item']

    def clean(self):
        if not self.checklist_id or not self.item_id:
            return  # field-level errors are already reported

        # Read-only once saved
        if not self._state.adding:
            raise ValidationError("Recorded entries are read-only.")

        # Closed or CTQ-frozen checklist accepts nothing
        if self.checklist.status != 'IN_PROGRESS':
            raise ValidationError("Checklist is closed or frozen; no further entries allowed.")

        # Locked step-by-step: every earlier step in the section must be recorded.
        # (A failed CTQ step freezes the whole checklist, so a non-CTQ failure
        # may continue and goes to the supervisor for a decision.)
        earlier = ChecklistItem.objects.filter(
            section=self.item.section, step_number__lt=self.item.step_number)
        recorded = InspectorEntry.objects.filter(
            checklist=self.checklist).values_list('item_id', flat=True)
        missing = earlier.exclude(id__in=recorded).first()
        if missing:
            raise ValidationError(f"Workflow locked: complete step {missing.step_number} first.")

        if self.item.check_type == ChecklistItem.NUMERIC:
            if self.actual_value is None:
                raise ValidationError("A numeric value is required.")
            if self.gauge is None:
                raise ValidationError("Select the gauge used for this measurement.")
            if not self.gauge.is_calibrated():
                raise ValidationError(f"Expired Tool Calibration: {self.gauge.gauge_id}.")
            lo, hi = self.item.min_tolerance, self.item.max_tolerance
            self.is_checked = ((lo is None or self.actual_value >= lo) and
                               (hi is None or self.actual_value <= hi))
        else:
            if self.visual_ok is None:
                raise ValidationError("Visual pass/fail answer is required.")
            self.is_checked = self.visual_ok

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            # CTQ failure freezes the checklist immediately
            if self.item.is_ctq and not self.is_checked:
                self.checklist.status = 'FAILED'
                self.checklist.save(update_fields=['status', 'updated_at'])

    def delete(self, *args, **kwargs):
        raise ValidationError("Recorded entries cannot be deleted.")


class Review(models.Model):
    """Supervisor decision on a closed checklist (BRD Section 6). One per checklist, write-once."""
    APPROVED, CONCESSION, QUARANTINE = 'APPROVED', 'CONCESSION', 'QUARANTINE'
    DECISIONS = [
        (APPROVED, 'Approved'),
        (CONCESSION, 'Conditional Concession Release'),
        (QUARANTINE, 'Quarantine / Rejection Holding Area'),
    ]
    REASON_CODES = [
        ('CUSTOMER_USE', 'Acceptable for customer use'),
        ('FUNCTIONAL_OK', 'Functionally acceptable'),
        ('COSMETIC', 'Minor cosmetic deviation'),
        ('OTHER', 'Other (explain in justification)'),
    ]
    # decision -> resulting checklist status
    RESULT_STATUS = {APPROVED: 'APPROVED', CONCESSION: 'CONCESSION', QUARANTINE: 'QUARANTINED'}

    checklist = models.OneToOneField(PDIChecklist, on_delete=models.PROTECT, related_name='review')
    decision = models.CharField(max_length=20, choices=DECISIONS)
    reason_code = models.CharField(max_length=20, choices=REASON_CODES, blank=True)
    justification = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name='+')
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [('can_review_pdi', 'Can review PDI checklists')]

    def clean(self):
        if not self.checklist_id or not self.reviewed_by_id:
            return  # field-level errors are already reported
        if not self._state.adding:
            raise ValidationError("Reviews are permanent and cannot be edited.")

        cl, user = self.checklist, self.reviewed_by
        if cl.status not in ('PASSED', 'FAILED'):
            raise ValidationError("Only submitted (or CTQ-frozen) checklists can be reviewed.")
        if not user.has_perm('quality.can_review_pdi'):
            raise ValidationError("Only an authorised supervisor can review checklists.")
        if user.pk == cl.inspector_id:
            raise ValidationError("The inspector cannot review their own checklist.")

        failing = cl.entries.filter(is_checked=False)
        complete = cl.entries.count() >= ChecklistItem.objects.count()
        ctq_failed = failing.filter(item__is_ctq=True).exists()

        if self.decision == self.APPROVED:
            if not complete or failing.exists():
                raise ValidationError("Cannot approve: the checklist is incomplete or has out-of-tolerance entries.")
        elif self.decision == self.CONCESSION:
            if ctq_failed:
                raise ValidationError("CTQ failures cannot be released on concession.")
            if not complete:
                raise ValidationError("Cannot grant a concession on an incomplete checklist.")
            if not failing.exists():
                raise ValidationError("Nothing to concede: approve the checklist instead.")
            if not self.reason_code:
                raise ValidationError("A concession reason code is required.")
            if not self.justification.strip():
                raise ValidationError("A detailed engineering justification is required.")
        # QUARANTINE is allowed for any submitted / frozen checklist.

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.checklist.status = self.RESULT_STATUS[self.decision]
            self.checklist.save(update_fields=['status', 'updated_at'])

    def delete(self, *args, **kwargs):
        raise ValidationError("Reviews cannot be deleted.")


class GatePass(models.Model):
    """Dispatch gate pass (BRD Section 10). Locked unless the PDI is cleared by a supervisor."""
    CLEARED_STATUSES = ('APPROVED', 'CONCESSION')
    BLOCK_MESSAGES = {
        'IN_PROGRESS': "Final PDI checklist is not completed.",
        'PASSED': "PDI checklist is complete but still awaiting supervisor approval.",
        'FAILED': "PDI checklist failed and is awaiting a supervisor decision.",
        'QUARANTINED': "Lot is quarantined in the Rejection Holding Area (pending MRB disposition).",
    }

    checklist = models.OneToOneField(PDIChecklist, on_delete=models.PROTECT, related_name='gate_pass')
    customer_po = models.CharField(max_length=100)
    vehicle_details = models.CharField(max_length=100, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                  related_name='+')
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [('can_issue_gate_pass', 'Can issue dispatch gate passes')]

    @classmethod
    def blockers(cls, checklist):
        """Return the list of unmet requirements (empty list = cleared to ship)."""
        if checklist.status in cls.CLEARED_STATUSES:
            return []
        return [cls.BLOCK_MESSAGES.get(checklist.status, "Lot is not cleared for dispatch.")]

    @property
    def number(self):
        return f"GP-{self.pk:06d}" if self.pk else None

    @property
    def released_on_concession(self):
        return self.checklist.status == 'CONCESSION'

    def clean(self):
        if not self.checklist_id or not self.issued_by_id:
            return  # field-level errors are already reported
        if not self._state.adding:
            raise ValidationError("Gate passes are permanent and cannot be edited.")
        if not self.issued_by.has_perm('quality.can_issue_gate_pass'):
            raise ValidationError("You are not authorised to issue gate passes.")
        blockers = self.blockers(self.checklist)
        if blockers:
            raise ValidationError(["Shipping Interlock: " + b for b in blockers])

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Gate passes cannot be deleted.")