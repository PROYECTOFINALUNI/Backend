from django import forms
from django.contrib import admin
from django.db.models import Count

from expenses.models import (
    CategoryField,
    Expense,
    ExpenseCategory,
    ExpenseFieldValue,
    ExpenseReport,
    ExpenseWarning,
    WarningRule,
)
from expenses.services.expenses import _replace_warnings
from expenses.services.money import calculate_tax


class ExpenseAdminForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = (
            "user",
            "report",
            "merchant",
            "category",
            "expense_date",
            "currency",
            "amount_minor",
            "tax_rate_bps",
            "tax_included",
        )

    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get("amount_minor")
        rate = cleaned_data.get("tax_rate_bps")
        included = cleaned_data.get("tax_included")
        if amount is not None and rate is not None and included is not None:
            try:
                self._calculation = calculate_tax(amount, rate, tax_included=included)
            except (TypeError, ValueError, OverflowError) as exc:
                raise forms.ValidationError(str(exc)) from exc
        return cleaned_data

    def save(self, commit=True):
        expense = super().save(commit=False)
        calculation = getattr(self, "_calculation", None)
        if calculation is not None:
            expense.tax_amount_minor = calculation.tax_minor
            expense.total_amount_minor = calculation.total_minor
        if commit:
            expense.save()
            self.save_m2m()
        return expense


class CategoryFieldInlineForm(forms.ModelForm):
    """Applies the same protections here that the API enforces on a field already in use."""

    class Meta:
        model = CategoryField
        fields = ("name", "field_type", "required", "active", "display_order")

    def _in_use(self):
        return bool(self.instance.pk) and self.instance.expense_values.exists()

    def clean_field_type(self):
        # Runs before _post_clean, so self.instance still holds the stored type.
        field_type = self.cleaned_data["field_type"]
        if field_type != self.instance.field_type and self._in_use():
            raise forms.ValidationError(
                "This field already has saved values, so its type cannot change."
            )
        return field_type

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("DELETE") and self._in_use():
            raise forms.ValidationError(
                "This field already has saved values. Untick Active instead of deleting it."
            )
        return cleaned_data


class CategoryFieldInline(admin.TabularInline):
    model = CategoryField
    form = CategoryFieldInlineForm
    extra = 0


class ExpenseFieldValueInline(admin.TabularInline):
    model = ExpenseFieldValue
    extra = 0
    fields = ("field", "value_text", "value_number", "value_boolean")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "active", "custom_field_count")
    list_filter = ("active",)
    search_fields = ("code", "name")
    readonly_fields = ("created_at", "updated_at")
    inlines = (CategoryFieldInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_custom_field_count=Count("custom_fields"))

    @admin.display(description="Custom fields", ordering="_custom_field_count")
    def custom_field_count(self, obj):
        return obj._custom_field_count


@admin.register(ExpenseReport)
class ExpenseReportAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "status", "submitted_at", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "user__email", "user__full_name")
    list_select_related = ("user",)
    readonly_fields = ("status", "submitted_at", "created_at", "updated_at")
    autocomplete_fields = ("user",)
    ordering = ("-created_at",)

    @admin.display(description="Owner", ordering="user__email")
    def owner(self, obj):
        return obj.user

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("user")
        return fields

    def has_change_permission(self, request, obj=None):
        allowed = super().has_change_permission(request, obj)
        return allowed and (obj is None or obj.status == ExpenseReport.Status.DRAFT)

    def has_delete_permission(self, request, obj=None):
        allowed = super().has_delete_permission(request, obj)
        return allowed and (
            obj is None
            or (
                obj.status == ExpenseReport.Status.DRAFT
                and obj.submitted_at is None
                and not obj.approval_events.exists()
            )
        )


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    form = ExpenseAdminForm
    list_display = (
        "merchant",
        "report",
        "user",
        "expense_date",
        "currency",
        "total_amount_minor",
        "status",
    )
    list_filter = ("status", "currency", "category")
    search_fields = ("merchant",)
    list_select_related = ("report", "user", "category")
    autocomplete_fields = ("user", "report", "category")
    readonly_fields = (
        "tax_amount_minor",
        "total_amount_minor",
        "status",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "expense_date"
    inlines = (ExpenseFieldValueInline,)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _replace_warnings(obj)

    def has_change_permission(self, request, obj=None):
        allowed = super().has_change_permission(request, obj)
        return allowed and (obj is None or obj.report.status == ExpenseReport.Status.DRAFT)

    def has_delete_permission(self, request, obj=None):
        allowed = super().has_delete_permission(request, obj)
        return allowed and (obj is None or obj.report.status == ExpenseReport.Status.DRAFT)


@admin.register(WarningRule)
class WarningRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "severity",
        "currency",
        "threshold_minor",
        "category",
        "active",
    )
    list_filter = ("active", "severity", "currency", "category")
    search_fields = ("name", "message")
    autocomplete_fields = ("category",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ExpenseWarning)
class ExpenseWarningAdmin(admin.ModelAdmin):
    list_display = ("expense", "severity", "message", "warning_rule", "created_at")
    list_filter = ("severity",)
    search_fields = ("expense__merchant", "message")
    list_select_related = ("expense", "warning_rule")
    ordering = ("-created_at",)

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
