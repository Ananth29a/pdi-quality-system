from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .models import ChecklistItem, Gauge, GatePass, InspectorEntry, PDIChecklist, Review


class StampUserMixin:
    """Sets the logged-in user on new records BEFORE validation, so model rules
    (permissions, 'inspector cannot review own checklist') are checked on the admin form."""
    stamp_field = None

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj, **kwargs)
        stamp_field = self.stamp_field

        class StampedForm(form_class):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                if obj is None:
                    setattr(self.instance, stamp_field, request.user)

        return StampedForm


class WriteOnceMixin:
    """Records can be added and viewed, but never edited or deleted (audit trail)."""

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Gauge)
class GaugeAdmin(admin.ModelAdmin):
    list_display = ('gauge_id', 'description', 'calibration_expiry', 'calibrated')
    search_fields = ('gauge_id',)

    @admin.display(boolean=True, description='Calibrated')
    def calibrated(self, obj):
        return obj.is_calibrated()


@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    list_display = ('step_number', 'parameter_name', 'section', 'check_type',
                    'is_ctq', 'min_tolerance', 'max_tolerance')
    list_filter = ('section', 'is_ctq', 'check_type')


@admin.register(PDIChecklist)
class PDIChecklistAdmin(StampUserMixin, admin.ModelAdmin):
    stamp_field = 'inspector'
    exclude = ('inspector',)
    list_display = ('id', 'asset_serial_number', 'inspector', 'status', 'started_at')
    list_filter = ('status',)
    actions = ['submit_checklists']

    def get_readonly_fields(self, request, obj=None):
        fields = ['status', 'started_at', 'updated_at']
        return fields + ['asset_serial_number'] if obj else fields

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Submit selected checklists (inspector done)")
    def submit_checklists(self, request, queryset):
        for checklist in queryset:
            try:
                checklist.submit()
                self.message_user(request, f"{checklist}: submitted as {checklist.status}.")
            except ValidationError as e:
                self.message_user(request, f"{checklist}: {'; '.join(e.messages)}",
                                  level=messages.ERROR)


@admin.register(InspectorEntry)
class InspectorEntryAdmin(StampUserMixin, WriteOnceMixin, admin.ModelAdmin):
    stamp_field = 'entered_by'
    exclude = ('entered_by', 'is_checked')   # result is computed by the model, never typed
    list_display = ('checklist', 'item', 'actual_value', 'gauge', 'is_checked', 'entered_by', 'entered_at')
    list_filter = ('is_checked',)


@admin.register(Review)
class ReviewAdmin(StampUserMixin, WriteOnceMixin, admin.ModelAdmin):
    stamp_field = 'reviewed_by'
    exclude = ('reviewed_by',)
    list_display = ('checklist', 'decision', 'reason_code', 'reviewed_by', 'reviewed_at')
    list_filter = ('decision',)


@admin.register(GatePass)
class GatePassAdmin(StampUserMixin, WriteOnceMixin, admin.ModelAdmin):
    stamp_field = 'issued_by'
    exclude = ('issued_by',)
    list_display = ('number', 'checklist', 'customer_po', 'released_on_concession', 'issued_by', 'issued_at')
