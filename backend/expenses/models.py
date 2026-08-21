import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from common.models import TimeStampedModel, foreign_key_changed
from expenses.services.money import validate_currency


class ExpenseCategory(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def clean(self):
        super().clean()
        self.code = self.code.strip().upper()
        if not self.code:
            raise ValidationError({"code": "Category code cannot be empty."})

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class CategoryField(TimeStampedModel):
    """Define un campo personalizado asociado a una categoría. Si el campo ya tiene valores almacenados, puede renombrarse, reordenarse o desactivarse, pero no eliminarse ni cambiar de tipo.
    """

    class FieldType(models.TextChoices):
        TEXT = "TEXT", "Text"
        NUMBER = "NUMBER", "Number"
        BOOLEAN = "BOOLEAN", "Boolean"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.CASCADE,
        related_name="custom_fields",
    )
    name = models.CharField(max_length=100)
    field_type = models.CharField(max_length=16, choices=FieldType.choices)
    required = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "category",
                name="category_field_name_ci_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["category", "active"], name="category_field_lookup_idx"),
        ]

    def clean(self):
        super().clean()
        self.name = self.name.strip()
        if not self.name:
            raise ValidationError({"name": "Field name cannot be empty."})

    def __str__(self) -> str:
        return f"{self.name} ({self.get_field_type_display()})"


class ExpenseReport(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="expense_reports",
    )
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["submitted_at"], name="report_submitted_at_idx"),
        ]

    def __str__(self) -> str:
        return self.title


class Expense(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    report = models.ForeignKey(
        ExpenseReport,
        on_delete=models.CASCADE,
        related_name="expenses",
        null=False,
        blank=False,
    )
    merchant = models.CharField(max_length=200)
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
        null=True,
        blank=True,
    )
    expense_date = models.DateField()
    currency = models.CharField(max_length=3)
    amount_minor = models.BigIntegerField()
    tax_rate_bps = models.PositiveIntegerField(default=0)
    tax_amount_minor = models.BigIntegerField(default=0)
    total_amount_minor = models.BigIntegerField()
    tax_included = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_minor__gte=0),
                name="expense_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(tax_amount_minor__gte=0),
                name="expense_tax_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(total_amount_minor__gte=0),
                name="expense_total_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(tax_rate_bps__gte=0, tax_rate_bps__lte=10000),
                name="expense_tax_bps_range",
            ),
        ]
        indexes = [
            models.Index(fields=["report", "status"], name="expense_report_status_idx"),
            models.Index(fields=["user", "expense_date"], name="expense_user_date_idx"),
            models.Index(fields=["category"], name="expense_category_idx"),
            models.Index(fields=["currency"], name="expense_currency_idx"),
            models.Index(fields=["created_at"], name="expense_created_at_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        self.merchant = self.merchant.strip()
        if not self.merchant:
            errors["merchant"] = "Merchant cannot be empty."
        try:
            self.currency = validate_currency(self.currency)
        except (TypeError, ValueError) as exc:
            errors["currency"] = str(exc)
        if self.report_id and self.user_id:
            report_user_id = (
                self.report.user_id
                if "report" in self._state.fields_cache
                else ExpenseReport.objects.filter(pk=self.report_id)
                .values_list("user_id", flat=True)
                .first()
            )
            if report_user_id != self.user_id:
                errors["user"] = "Expense user must match the report owner."
        if self.category_id and foreign_key_changed(self, "category"):
            category_active = (
                self.category.active
                if "category" in self._state.fields_cache
                else ExpenseCategory.objects.filter(pk=self.category_id)
                .values_list("active", flat=True)
                .first()
            )
            if category_active is False:
                errors["category"] = "A new category selection must be active."
        if errors:
            raise ValidationError(errors)


class ExpenseFieldValue(TimeStampedModel):
    """Almacena el valor de un campo personalizado asociado a un gasto. Solo se utiliza la columna correspondiente al tipo de campo y no se permite eliminar una definición que ya tenga valores asociados.
    """

    VALUE_COLUMNS = {
        CategoryField.FieldType.TEXT: "value_text",
        CategoryField.FieldType.NUMBER: "value_number",
        CategoryField.FieldType.BOOLEAN: "value_boolean",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="field_values")
    field = models.ForeignKey(
        CategoryField,
        on_delete=models.PROTECT,
        related_name="expense_values",
    )
    value_text = models.CharField(max_length=500, blank=True, default="")
    value_number = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    value_boolean = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ["field__display_order", "field__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["expense", "field"],
                name="expense_field_value_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["field"], name="expense_field_value_field_idx"),
        ]

    @property
    def value(self):
        return getattr(self, self.VALUE_COLUMNS[self.field.field_type])

    def clean(self):
        super().clean()
        if not self.field_id:
            return
        field_type = (
            self.field.field_type
            if "field" in self._state.fields_cache
            else CategoryField.objects.filter(pk=self.field_id)
            .values_list("field_type", flat=True)
            .first()
        )
        if field_type is None:
            return
        expected = self.VALUE_COLUMNS[field_type]
        errors = {}
        if self._is_unset(expected):
            errors[expected] = f"A {field_type.lower()} value is required."
        for column in self.VALUE_COLUMNS.values():
            if column != expected and not self._is_unset(column):
                errors[column] = "Only the column matching the field type may be set."
        if errors:
            raise ValidationError(errors)

    def _is_unset(self, column):
# False y 0 son valores válidos; solo None y una cadena vacía se consideran sin valor.
        value = getattr(self, column)
        return value is None or value == ""

    def __str__(self) -> str:
        return f"{self.field_id} = {self.value_text or self.value_number or self.value_boolean}"


class WarningRule(TimeStampedModel):
    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        BLOCKING = "BLOCKING", "Blocking"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="warning_rules",
        null=True,
        blank=True,
    )
    currency = models.CharField(max_length=3)
    threshold_minor = models.BigIntegerField()
    severity = models.CharField(max_length=16, choices=Severity.choices)
    message = models.CharField(max_length=255)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(threshold_minor__gte=0),
                name="warning_rule_threshold_nonnegative",
            ),
        ]
        indexes = [
            models.Index(fields=["active"], name="warning_rule_active_idx"),
            models.Index(fields=["currency"], name="warning_rule_currency_idx"),
            models.Index(fields=["category"], name="warning_rule_category_idx"),
            models.Index(
                fields=["active", "currency", "category"],
                name="warning_rule_lookup_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        try:
            self.currency = validate_currency(self.currency)
        except (TypeError, ValueError) as exc:
            errors["currency"] = str(exc)
        if self.category_id and foreign_key_changed(self, "category"):
            category_active = (
                self.category.active
                if "category" in self._state.fields_cache
                else ExpenseCategory.objects.filter(pk=self.category_id)
                .values_list("active", flat=True)
                .first()
            )
            if category_active is False:
                errors["category"] = "A warning rule must use an active category."
        if errors:
            raise ValidationError(errors)


class ExpenseWarning(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="warnings")
    warning_rule = models.ForeignKey(
        WarningRule,
        on_delete=models.SET_NULL,
        related_name="expense_warnings",
        null=True,
        blank=True,
    )
    severity = models.CharField(max_length=16, choices=WarningRule.Severity.choices)
    message = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["expense", "warning_rule"],
                name="expense_warning_rule_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["expense", "created_at"],
                name="expense_warning_created_idx",
            ),
            models.Index(fields=["warning_rule"], name="expense_warning_rule_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.severity} — {self.expense_id}"
