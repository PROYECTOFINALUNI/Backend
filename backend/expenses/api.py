from collections import defaultdict

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.db.models.deletion import ProtectedError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_field,
    extend_schema_view,
)
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from approvals import services as workflow
from approvals.models import ApprovalEvent, ApprovalStep, ApprovalStepCandidate
from approvals.services.rules import plan_chain
from common.api import (
    DomainConflict,
    StrictFieldsMixin,
    StrictSerializer,
    StrictStringField,
)
from expenses.models import (
    CategoryField,
    Expense,
    ExpenseCategory,
    ExpenseReport,
    WarningRule,
)
from expenses.services import custom_fields as custom_field_services
from expenses.services import expenses as expense_services
from expenses.services import reports as report_services
from expenses.services.money import MoneyDTO, check_minor_amount, validate_currency
from users.api import IsAdmin, UserReadSerializer
from users.models import User


def is_admin(user):
    return user.role == User.Role.ADMIN


def _is_truthy(value):
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def visible_reports(user):
    queryset = ExpenseReport.objects.all()
    if not is_admin(user):
# Un informe sigue siendo visible para los usuarios que hayan participado previamente en su proceso de aprobación, aunque hayan delegado o sido sustituidos
        queryset = queryset.filter(
            Q(user=user) | Q(approval_steps__candidates__user=user) | Q(approval_events__actor=user)
        ).distinct()
    return queryset


def money(minor, currency):
    return MoneyDTO(minor, currency).as_dict()


def _field_in_use_conflict():
    return DomainConflict(
        "category_field_in_use",
        "A custom field that already has saved values cannot be removed. Deactivate it instead.",
    )


class CategoryFieldReadSerializer(serializers.ModelSerializer):
    in_use = serializers.SerializerMethodField()

    class Meta:
        model = CategoryField
        fields = [
            "id",
            "name",
            "field_type",
            "required",
            "active",
            "display_order",
            "in_use",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_in_use(self, obj):
        """Indica si el campo ya tiene valores asociados y, por tanto, su tipo no puede modificarse"""
# Normalmente este valor viene calculado previamente; si no, se consulta directamente.
        annotated = getattr(obj, "value_count", None)
        return bool(annotated) if annotated is not None else obj.expense_values.exists()


class CategoryFieldWriteSerializer(StrictFieldsMixin, serializers.ModelSerializer):
# El id solo se incluye al editar un campo existente.
# El orden de visualización viene determinado por la posición en la lista.
    id = serializers.UUIDField(required=False)

    class Meta:
        model = CategoryField
        fields = ["id", "name", "field_type", "required", "active"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Field name cannot be empty.")
        return value


class CategoryReadSerializer(serializers.ModelSerializer):
    custom_fields = CategoryFieldReadSerializer(many=True, read_only=True)

    class Meta:
        model = ExpenseCategory
        fields = [
            "id",
            "code",
            "name",
            "active",
            "custom_fields",
            "created_at",
            "updated_at",
        ]


class CategoryWriteSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    custom_fields = CategoryFieldWriteSerializer(many=True, required=False)

    class Meta:
        model = ExpenseCategory
        fields = ["code", "name", "active", "custom_fields"]

    def validate_code(self, value):
        return value.strip().upper()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name cannot be empty.")
        return value

    def validate_custom_fields(self, value):
        names = [field["name"].casefold() for field in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise serializers.ValidationError("Field names must be unique within a category.")
        return value

    def _save_validated(self, instance):
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance

    def _sync_custom_fields(self, category, payload):
        """Sincroniza los campos personalizados enviados con los ya almacenados.
    Los campos con id se actualizan, los que no tienen id se crean y los omitidos se eliminan. No se pueden eliminar campos que ya tengan valores asociados.
        """
        existing = {field.id: field for field in category.custom_fields.all()}
        submitted_ids = set()

        for order, item in enumerate(payload):
            field_id = item.get("id")
            field = existing.get(field_id) if field_id else None
            if field_id and field is None:
                raise serializers.ValidationError(
                    {"custom_fields": f"Field {field_id} does not belong to this category."}
                )
            if field is None:
                field = CategoryField(category=category)
            elif field.field_type != item["field_type"] and field.expense_values.exists():
                raise serializers.ValidationError(
                    {
                        "custom_fields": (
                            f"'{field.name}' already has saved values, so its type cannot "
                            "change. Deactivate it and add a new field instead."
                        )
                    }
                )
            field.name = item["name"]
            field.field_type = item["field_type"]
            field.required = item.get("required", False)
            field.active = item.get("active", True)
            field.display_order = order
            self._save_validated(field)
            submitted_ids.add(field.id)

        for field in existing.values():
            if field.id not in submitted_ids:
                field.delete()

    def create(self, validated_data):
        payload = validated_data.pop("custom_fields", [])
        category = self._save_validated(ExpenseCategory(**validated_data))
        self._sync_custom_fields(category, payload)
        return category

    def update(self, instance, validated_data):
        payload = validated_data.pop("custom_fields", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        self._save_validated(instance)
        if payload is not None:
            self._sync_custom_fields(instance, payload)
        return instance


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = ExpenseCategory.objects.all()
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        return [IsAuthenticated()] if self.action in {"list", "retrieve"} else [IsAdmin()]

    def get_queryset(self):
        # annotate() elimina el orden por defecto del modelo, por lo que debe indicarse de nuevo.
        fields = CategoryField.objects.annotate(value_count=Count("expense_values")).order_by(
            "display_order", "name"
        )
        queryset = super().get_queryset().prefetch_related(Prefetch("custom_fields", fields))
        return queryset if is_admin(self.request.user) else queryset.filter(active=True)

    def get_serializer_class(self):
        return (
            CategoryReadSerializer
            if self.action in {"list", "retrieve"}
            else CategoryWriteSerializer
        )

    def _read_response(self, category, status_code=status.HTTP_200_OK):
        category = self.get_queryset().get(pk=category.pk)
        return Response(CategoryReadSerializer(category).data, status=status_code)

    def _save(self, serializer):
# Si falla la actualización de algún campo, se revierten los cambios anteriores
# para evitar que la categoría quede actualizada parcialmente.
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                return serializer.save()
        except ProtectedError as exc:
            raise _field_in_use_conflict() from exc

    def create(self, request, *args, **kwargs):
        category = self._save(self.get_serializer(data=request.data))
        return self._read_response(category, status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        return self._read_response(self._save(serializer))

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise DomainConflict(
                "category_in_use", "Referenced categories cannot be deleted."
            ) from exc


class WarningRuleReadSerializer(serializers.ModelSerializer):
    threshold_minor = serializers.SerializerMethodField()
    category_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = WarningRule
        fields = [
            "id",
            "name",
            "category_id",
            "currency",
            "threshold_minor",
            "severity",
            "message",
            "active",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_threshold_minor(self, obj):
        return str(obj.threshold_minor)


class WarningRuleWriteSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    threshold_minor = StrictStringField()
    currency = StrictStringField(max_length=3)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=ExpenseCategory.objects.filter(active=True),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = WarningRule
        fields = [
            "name",
            "category_id",
            "currency",
            "threshold_minor",
            "severity",
            "message",
            "active",
        ]

    def validate_currency(self, value):
        try:
            return validate_currency(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_threshold_minor(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("Expected a non-negative integer string.")
        try:
            return check_minor_amount(int(value))
        except (TypeError, OverflowError) as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name cannot be empty.")
        return value

    def validate_message(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Message cannot be empty.")
        return value

    def _save_validated(self, instance):
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance

    def create(self, validated_data):
        return self._save_validated(WarningRule(**validated_data))

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        return self._save_validated(instance)


class WarningRuleViewSet(viewsets.ModelViewSet):
    queryset = WarningRule.objects.select_related("category").order_by("name", "id")
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            return [IsAuthenticated()]
        return [IsAdmin()]

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset if is_admin(self.request.user) else queryset.filter(active=True)

    def get_serializer_class(self):
        return (
            WarningRuleReadSerializer
            if self.action in {"list", "retrieve"}
            else WarningRuleWriteSerializer
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        return Response(WarningRuleReadSerializer(rule).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        return Response(WarningRuleReadSerializer(rule).data)


class TaxInputSerializer(StrictSerializer):
    rate_bps = serializers.IntegerField(min_value=0, max_value=10000)
    included = serializers.BooleanField()


class ExpenseCustomFieldInputSerializer(StrictSerializer):
    """Representa el valor enviado para un campo personalizado de una categoría.
Se utiliza una lista para evitar que el identificador del campo sea modificado
por el conversor a camelCase.
    """

    field_id = serializers.UUIDField()
# El tipo del valor se valida posteriormente según la definición del campo.
    value = serializers.JSONField(allow_null=True)


class ExpenseWriteSerializer(StrictSerializer):
    report_id = serializers.UUIDField()
    merchant = serializers.CharField(max_length=200, trim_whitespace=True)
    expense_date = serializers.DateField()
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=ExpenseCategory.objects.filter(active=True), allow_null=True
    )
    currency = StrictStringField(max_length=3)
    amount_decimal = StrictStringField()
    tax = TaxInputSerializer()
    custom_fields = ExpenseCustomFieldInputSerializer(many=True, required=False)

    def validate_merchant(self, value):
        if not value.strip():
            raise serializers.ValidationError("Merchant cannot be empty.")
        return value.strip()

    def validate_currency(self, value):
        try:
            return validate_currency(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        try:
            expense_services.calculate_payload(
                currency=attrs["currency"],
                amount_decimal=attrs["amount_decimal"],
                tax=attrs["tax"],
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise serializers.ValidationError({"amountDecimal": str(exc)}) from exc
        return attrs


class ExpensePatchSerializer(ExpenseWriteSerializer):
    report_id = serializers.UUIDField(required=False)
    merchant = serializers.CharField(max_length=200, trim_whitespace=True, required=False)
    expense_date = serializers.DateField(required=False)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=ExpenseCategory.objects.filter(active=True),
        allow_null=True,
        required=False,
    )
    currency = StrictStringField(max_length=3, required=False)
    amount_decimal = StrictStringField(required=False)
    tax = TaxInputSerializer(required=False)

    def validate(self, attrs):
        if "report_id" in self.initial_data:
            raise serializers.ValidationError({"reportId": "reportId is immutable."})
        if "currency" in attrs and "amount_decimal" not in attrs:
            raise serializers.ValidationError(
                {"amountDecimal": "amountDecimal is required when currency changes."}
            )
        expense = self.context["expense"]
        currency = attrs.get("currency", expense.currency)
        amount_decimal = attrs.get("amount_decimal")
        if amount_decimal is None:
            from expenses.services.money import minor_to_decimal_string

            amount_decimal = minor_to_decimal_string(expense.amount_minor, expense.currency)
        tax = attrs.get(
            "tax",
            {"rate_bps": expense.tax_rate_bps, "included": expense.tax_included},
        )
        try:
            expense_services.calculate_payload(
                currency=currency, amount_decimal=amount_decimal, tax=tax
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise serializers.ValidationError({"amountDecimal": str(exc)}) from exc
        return attrs


class WarningSnapshotSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    severity = serializers.CharField()
    message = serializers.CharField()
    created_at = serializers.DateTimeField()


class MoneyReadSerializer(serializers.Serializer):
    currency = serializers.CharField()
    minor = serializers.CharField()
    decimal = serializers.CharField()
    display = serializers.CharField()


class TaxReadSerializer(serializers.Serializer):
    rate_bps = serializers.IntegerField()
    included = serializers.BooleanField()
    minor = serializers.CharField()
    decimal = serializers.CharField()
    display = serializers.CharField()


class PreviewWarningSerializer(serializers.Serializer):
    severity = serializers.CharField()
    message = serializers.CharField()


class ExpensePreviewSerializer(serializers.Serializer):
    amount = MoneyReadSerializer()
    net_amount = MoneyReadSerializer()
    tax = TaxReadSerializer()
    total = MoneyReadSerializer()
    warnings = PreviewWarningSerializer(many=True)


class ExpenseCustomFieldSerializer(serializers.Serializer):
    field_id = serializers.UUIDField()
    name = serializers.CharField()
    field_type = serializers.ChoiceField(choices=CategoryField.FieldType.choices)
    # Text, number and boolean values all arrive through this one key.
    value = serializers.JSONField()


class ExpenseReadSerializer(serializers.ModelSerializer):
    category = CategoryReadSerializer(allow_null=True)
    amount = serializers.SerializerMethodField()
    net_amount = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    tax = serializers.SerializerMethodField()
    warnings = WarningSnapshotSerializer(many=True)
    custom_fields = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = [
            "id",
            "report_id",
            "merchant",
            "expense_date",
            "category",
            "status",
            "amount",
            "net_amount",
            "tax",
            "total",
            "warnings",
            "custom_fields",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(ExpenseCustomFieldSerializer(many=True))
    def get_custom_fields(self, obj):
        return custom_field_services.read_values(obj)

    @extend_schema_field(MoneyReadSerializer)
    def get_amount(self, obj):
        return money(obj.amount_minor, obj.currency)

    @extend_schema_field(MoneyReadSerializer)
    def get_net_amount(self, obj):
        net_minor = (
            obj.amount_minor - obj.tax_amount_minor if obj.tax_included else obj.amount_minor
        )
        return money(net_minor, obj.currency)

    @extend_schema_field(MoneyReadSerializer)
    def get_total(self, obj):
        return money(obj.total_amount_minor, obj.currency)

    @extend_schema_field(TaxReadSerializer)
    def get_tax(self, obj):
        tax_money = money(obj.tax_amount_minor, obj.currency)
        tax_money.pop("currency")
        return {
            "rateBps": obj.tax_rate_bps,
            "included": obj.tax_included,
            **tax_money,
        }


def expense_queryset(user):
    reports = visible_reports(user).values("id")
    return (
        Expense.objects.filter(report_id__in=reports)
        .select_related("category", "report")
        .prefetch_related("warnings", "field_values__field", "category__custom_fields")
    )


@extend_schema_view(
    create=extend_schema(request=ExpenseWriteSerializer, responses=ExpenseReadSerializer),
    partial_update=extend_schema(request=ExpensePatchSerializer, responses=ExpenseReadSerializer),
    calculate_preview=extend_schema(
        request=ExpenseWriteSerializer, responses=ExpensePreviewSerializer
    ),
)
class ExpenseViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    queryset = Expense.objects.none()
    serializer_class = ExpenseReadSerializer

    def get_queryset(self):
        queryset = expense_queryset(self.request.user)
        params = self.request.query_params
        mapping = {
            "reportId": "report_id",
            "status": "status",
            "categoryId": "category_id",
            "currency": "currency",
            "expenseDateFrom": "expense_date__gte",
            "expenseDateTo": "expense_date__lte",
        }
        for parameter, lookup in mapping.items():
            if params.get(parameter):
                queryset = queryset.filter(**{lookup: params[parameter]})
        if params.get("merchant"):
            queryset = queryset.filter(merchant__icontains=params["merchant"])
        ordering = params.get("ordering", "-createdAt")
        descending = ordering.startswith("-")
        key = ordering.lstrip("-")
        allowed = {
            "expenseDate": "expense_date",
            "createdAt": "created_at",
            "total": "total_amount_minor",
        }
        field = allowed.get(key, "created_at")
        return queryset.order_by(("-" if descending else "") + field)

    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        if page is None:
            return Response(ExpenseReadSerializer(self.get_queryset(), many=True).data)
        return self.get_paginated_response(ExpenseReadSerializer(page, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(ExpenseReadSerializer(self.get_object()).data)

    def create(self, request):
        serializer = ExpenseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = (
            visible_reports(request.user).filter(pk=serializer.validated_data["report_id"]).first()
        )
        if report is None:
            raise NotFound()
        data = dict(serializer.validated_data)
        data.pop("report_id")
        expense = expense_services.create_expense(actor=request.user, report=report, data=data)
        return Response(ExpenseReadSerializer(expense).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        expense = self.get_object()
        serializer = ExpensePatchSerializer(
            data=request.data, partial=True, context={"expense": expense}
        )
        serializer.is_valid(raise_exception=True)
        expense = expense_services.update_expense(
            actor=request.user, expense=expense, data=serializer.validated_data
        )
        return Response(ExpenseReadSerializer(expense).data)

    def destroy(self, request, pk=None):
        expense_services.delete_expense(actor=request.user, expense=self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"], url_path="calculate-preview")
    def calculate_preview(self, request):
        serializer = ExpenseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = (
            visible_reports(request.user).filter(pk=serializer.validated_data["report_id"]).first()
        )
        if report is None:
            raise NotFound()
        data = dict(serializer.validated_data)
        data.pop("report_id")
        currency, amount_minor, calculation, rules = expense_services.preview_expense(
            actor=request.user, report=report, data=data
        )
        tax_money = money(calculation.tax_minor, currency)
        tax_money.pop("currency")
        return Response(
            {
                "amount": money(amount_minor, currency),
                "netAmount": money(calculation.net_minor, currency),
                "tax": {
                    "rateBps": data["tax"]["rate_bps"],
                    "included": data["tax"]["included"],
                    **tax_money,
                },
                "total": money(calculation.total_minor, currency),
                "warnings": [
                    {"severity": rule.severity, "message": rule.message} for rule in rules
                ],
            }
        )


class ApprovalStepReadSerializer(serializers.ModelSerializer):
    approvers = serializers.SerializerMethodField()
    decided_by = UserReadSerializer(allow_null=True)
    rule_name = serializers.CharField(source="rule.name", default=None, allow_null=True)

    class Meta:
        model = ApprovalStep
        fields = [
            "id",
            "approvers",
            "step_order",
            "status",
            "origin",
            "rule_name",
            "decided_by",
            "decided_at",
            "comment",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(UserReadSerializer(many=True))
    def get_approvers(self, obj):
        approvers = [candidate.user for candidate in obj.candidates.all()]
        return UserReadSerializer(approvers, many=True).data


def eligible_approvers():
    return User.objects.filter(is_active=True, role__in=[User.Role.APPROVER, User.Role.ADMIN])


class ApprovalStepWriteSerializer(StrictSerializer):
    approver_id = serializers.PrimaryKeyRelatedField(
        source="approver",
        queryset=eligible_approvers(),
    )
    step_order = serializers.IntegerField(min_value=1)


class DelegateSerializer(StrictSerializer):
    approver_id = serializers.PrimaryKeyRelatedField(
        source="approver",
        queryset=eligible_approvers(),
    )
    comment = serializers.CharField(allow_blank=True, trim_whitespace=True, required=False)


class PlannedStepSerializer(serializers.Serializer):
    step_order = serializers.IntegerField()
    rule_id = serializers.UUIDField()
    rule_name = serializers.CharField()
    approvers = UserReadSerializer(many=True)


class ApprovalPreviewSerializer(serializers.Serializer):
    auto_approve = serializers.BooleanField()
    manual_override = serializers.BooleanField()
    steps = PlannedStepSerializer(many=True)


class ApprovalEventSerializer(serializers.ModelSerializer):
    actor = UserReadSerializer()

    class Meta:
        model = ApprovalEvent
        fields = ["id", "actor", "action", "comment", "created_at"]


class ReportWriteSerializer(StrictSerializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True)

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Title cannot be empty.")
        return value


class CommentSerializer(StrictSerializer):
    comment = serializers.CharField(allow_blank=False, trim_whitespace=True)


class OptionalCommentSerializer(StrictSerializer):
    comment = serializers.CharField(allow_blank=True, trim_whitespace=True, required=False)


class EmptySerializer(StrictSerializer):
    pass


class ReportListSerializer(serializers.ModelSerializer):
    expense_count = serializers.IntegerField()
    totals = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseReport
        fields = [
            "id",
            "title",
            "status",
            "expense_count",
            "totals",
            "submitted_at",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(MoneyReadSerializer(many=True))
    def get_totals(self, obj):
        totals = defaultdict(int)
        for expense in obj.expenses.all():
            totals[expense.currency] += expense.total_amount_minor
        return [money(total, currency) for currency, total in sorted(totals.items())]


class ReportDetailSerializer(ReportListSerializer):
    owner = UserReadSerializer(source="user")
    expenses = ExpenseReadSerializer(many=True)
    approval_steps = ApprovalStepReadSerializer(many=True)
    events = serializers.SerializerMethodField()

    class Meta(ReportListSerializer.Meta):
        fields = ReportListSerializer.Meta.fields + [
            "owner",
            "expenses",
            "approval_steps",
            "events",
        ]

    @extend_schema_field(ApprovalEventSerializer(many=True))
    def get_events(self, obj):
        latest = list(obj.approval_events.all().order_by("-created_at", "-id")[:50])
        latest.sort(key=lambda event: (event.created_at, event.id))
        return ApprovalEventSerializer(latest, many=True).data


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                "role",
                OpenApiTypes.STR,
                enum=["owner", "approver"],
                description="Limit to reports the user owns or is an assigned approver on.",
            ),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                enum=ExpenseReport.Status.values,
                description="Filter by report status.",
            ),
            OpenApiParameter(
                "pendingMyApproval",
                OpenApiTypes.BOOL,
                description=(
                    "When true, return only submitted reports currently awaiting this "
                    "user's approval (they are the current approver in the ordered chain)."
                ),
            ),
        ],
        responses=ReportListSerializer,
    ),
    create=extend_schema(request=ReportWriteSerializer, responses=ReportListSerializer),
    partial_update=extend_schema(request=ReportWriteSerializer, responses=ReportDetailSerializer),
    submit=extend_schema(request=EmptySerializer, responses=ReportDetailSerializer),
    approve=extend_schema(request=OptionalCommentSerializer, responses=ReportDetailSerializer),
    reject=extend_schema(request=CommentSerializer, responses=ReportDetailSerializer),
    return_to_draft=extend_schema(request=EmptySerializer, responses=ReportDetailSerializer),
    mark_paid=extend_schema(request=EmptySerializer, responses=ReportDetailSerializer),
    comment=extend_schema(request=CommentSerializer, responses=ReportDetailSerializer),
    delegate=extend_schema(request=DelegateSerializer, responses=ReportDetailSerializer),
)
class ReportViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    queryset = ExpenseReport.objects.none()
    serializer_class = ReportDetailSerializer

    def get_permissions(self):
        manages_steps = self.action in {"approval_steps", "approval_step_detail"}
        if manages_steps and self.request.method != "GET":
            return [IsAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        queryset = (
            visible_reports(user)
            .select_related("user")
            .annotate(expense_count=Count("expenses", distinct=True))
            .prefetch_related(
                "expenses__category",
                "expenses__warnings",
                "approval_steps__rule",
                "approval_steps__decided_by",
                "approval_steps__candidates__user",
                "approval_events__actor",
            )
            .order_by("-created_at")
        )
        return self._apply_filters(queryset, user)

    def _apply_filters(self, queryset, user):
        params = self.request.query_params

        role = params.get("role")
        if role == "owner":
            queryset = queryset.filter(user=user)
        elif role == "approver":
            queryset = queryset.filter(approval_steps__candidates__user=user).distinct()

        status_param = params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)

        if _is_truthy(params.get("pendingMyApproval")):
# El informe espera la decisión del usuario si pertenece al paso de aprobación pendiente actual
            current_step = Subquery(
                ApprovalStep.objects.filter(
                    report=OuterRef("pk"), status=ApprovalStep.Status.PENDING
                )
                .order_by("step_order")
                .values("id")[:1]
            )
            queryset = (
                queryset.filter(status=ExpenseReport.Status.SUBMITTED)
                .annotate(current_step_id=current_step)
                .filter(
                    current_step_id__in=ApprovalStepCandidate.objects.filter(user=user).values(
                        "step_id"
                    )
                )
            )

        return queryset

    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        if page is None:
            return Response(ReportListSerializer(self.get_queryset(), many=True).data)
        return self.get_paginated_response(ReportListSerializer(page, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(ReportDetailSerializer(self.get_object()).data)

    def create(self, request):
        serializer = ReportWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = report_services.create_report(actor=request.user, **serializer.validated_data)
        report.expense_count = 0
        return Response(ReportListSerializer(report).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        serializer = ReportWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        report = self.get_object()
        report_services.update_report(
            report_id=report.id, actor=request.user, data=serializer.validated_data
        )
        report = self.get_queryset().get(pk=report.id)
        return Response(ReportDetailSerializer(report).data)

    def destroy(self, request, pk=None):
        report = self.get_object()
        report_services.delete_report(report_id=report.id, actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _workflow(self, request, function, serializer_class=EmptySerializer):
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = function(
            report_id=self.get_object().id,
            actor=request.user,
            **serializer.validated_data,
        )
        report = self.get_queryset().get(pk=report.pk)
        return Response(ReportDetailSerializer(report).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        return self._workflow(request, workflow.submit)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._workflow(request, workflow.approve, OptionalCommentSerializer)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._workflow(request, workflow.reject, CommentSerializer)

    @action(detail=True, methods=["post"], url_path="return-to-draft")
    def return_to_draft(self, request, pk=None):
        return self._workflow(request, workflow.return_to_draft)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        return self._workflow(request, workflow.mark_paid)

    @action(detail=True, methods=["post"])
    def comment(self, request, pk=None):
        return self._workflow(request, workflow.add_comment, CommentSerializer)

    @action(detail=True, methods=["post"])
    def delegate(self, request, pk=None):
        return self._workflow(request, workflow.delegate, DelegateSerializer)

    @extend_schema(responses=ApprovalPreviewSerializer)
    @action(detail=True, methods=["get"], url_path="approval-preview")
    def approval_preview(self, request, pk=None):
        report = self.get_object()
        manual_override = report.approval_steps.filter(origin=ApprovalStep.Origin.MANUAL).exists()
        planned = plan_chain(report)
        return Response(
            {
                "autoApprove": not planned and not manual_override,
                "manualOverride": manual_override,
                "steps": [
                    {
                        "stepOrder": step_order,
                        "ruleId": entry.rule.id,
                        "ruleName": entry.rule.name,
                        "approvers": UserReadSerializer(entry.approvers, many=True).data,
                    }
                    for step_order, entry in enumerate(planned, start=1)
                ],
            }
        )

    @action(detail=True, methods=["get", "post"], url_path="approval-steps")
    def approval_steps(self, request, pk=None):
        report = self.get_object()
        if request.method == "GET":
            return Response(ApprovalStepReadSerializer(report.approval_steps.all(), many=True).data)
        serializer = ApprovalStepWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        step = workflow.create_step(
            report_id=report.id,
            actor=request.user,
            **serializer.validated_data,
        )
        return Response(ApprovalStepReadSerializer(step).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[OpenApiParameter("step_id", OpenApiTypes.UUID, OpenApiParameter.PATH)]
    )
    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"approval-steps/(?P<step_id>[^/.]+)",
    )
    def approval_step_detail(self, request, pk=None, step_id=None):
        report = self.get_object()
        if request.method == "DELETE":
            deleted = workflow.delete_step(report_id=report.id, step_id=step_id, actor=request.user)
            if not deleted:
                raise NotFound()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = ApprovalStepWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        step = workflow.update_step(
            report_id=report.id,
            step_id=step_id,
            actor=request.user,
            data=serializer.validated_data,
        )
        if step is None:
            raise NotFound()
        return Response(ApprovalStepReadSerializer(step).data)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        return Response(
            ApprovalEventSerializer(self.get_object().approval_events.all(), many=True).data
        )
