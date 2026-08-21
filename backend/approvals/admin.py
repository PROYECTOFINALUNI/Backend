from django import forms
from django.contrib import admin

from approvals.models import (
    ApprovalEvent,
    ApprovalRule,
    ApprovalStep,
    ApprovalStepCandidate,
)
from expenses.models import ExpenseReport
from users.models import User


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "priority", "currency", "threshold_minor", "category", "active")
    list_filter = ("active", "currency", "approver_role")
    search_fields = ("name",)
    ordering = ("priority", "name")
    autocomplete_fields = (
        "category",
        "submitter_location",
        "submitter_department",
        "submitter_position",
        "approver_location",
        "approver_department",
        "approver_position",
    )


class ApprovalStepCandidateForm(forms.ModelForm):
    class Meta:
        model = ApprovalStepCandidate
        fields = ("user",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(
            is_active=True,
            role__in=(User.Role.APPROVER, User.Role.ADMIN),
        ).order_by("email")


class ApprovalStepCandidateInline(admin.TabularInline):
    model = ApprovalStepCandidate
    form = ApprovalStepCandidateForm
    extra = 1


class ApprovalStepAdminForm(forms.ModelForm):
    class Meta:
        model = ApprovalStep
        fields = ("report", "step_order")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["report"].queryset = ExpenseReport.objects.filter(
            status=ExpenseReport.Status.DRAFT
        )

    def clean(self):
        cleaned_data = super().clean()
        report = cleaned_data.get("report")
        if report and report.status != ExpenseReport.Status.DRAFT:
            self.add_error("report", "Approval steps may only be managed for draft reports.")
        return cleaned_data


@admin.register(ApprovalStep)
class ApprovalStepAdmin(admin.ModelAdmin):
    form = ApprovalStepAdminForm
    inlines = (ApprovalStepCandidateInline,)
    list_display = ("report", "step_order", "status", "origin", "decided_by", "decided_at")
    list_filter = ("status", "origin")
    search_fields = ("report__title", "candidates__user__email")
    list_select_related = ("report", "decided_by")
    autocomplete_fields = ("report",)
    readonly_fields = (
        "status",
        "origin",
        "rule",
        "decided_by",
        "decided_at",
        "comment",
        "created_at",
        "updated_at",
    )
    ordering = ("report", "step_order")

    def has_change_permission(self, request, obj=None):
        allowed = super().has_change_permission(request, obj)
        return allowed and (obj is None or obj.report.status == ExpenseReport.Status.DRAFT)

    def has_delete_permission(self, request, obj=None):
        allowed = super().has_delete_permission(request, obj)
        return allowed and (obj is None or obj.report.status == ExpenseReport.Status.DRAFT)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(ApprovalEvent)
class ApprovalEventAdmin(admin.ModelAdmin):
    list_display = ("report", "action", "actor", "created_at")
    list_filter = ("action",)
    search_fields = ("report__title", "actor__email", "action")
    list_select_related = ("report", "actor")
    ordering = ("-created_at", "-id")

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
