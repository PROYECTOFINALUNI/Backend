from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from approvals.models import ApprovalRule
from approvals.services.rules import resolve_pool
from common.api import StrictFieldsMixin, StrictStringField
from expenses.models import ExpenseCategory
from expenses.services.money import check_minor_amount, validate_currency
from users.api import IsAdmin, org_attribute_field
from users.models import OrgAttribute, User

CRITERIA_FIELDS = [
    "submitter_location_id",
    "submitter_department_id",
    "submitter_position_id",
    "approver_role",
    "approver_location_id",
    "approver_department_id",
    "approver_position_id",
]


class ApprovalRuleReadSerializer(serializers.ModelSerializer):
    threshold_minor = serializers.SerializerMethodField()
    category_id = serializers.UUIDField(allow_null=True)
    submitter_location_id = serializers.UUIDField(allow_null=True)
    submitter_department_id = serializers.UUIDField(allow_null=True)
    submitter_position_id = serializers.UUIDField(allow_null=True)
    approver_location_id = serializers.UUIDField(allow_null=True)
    approver_department_id = serializers.UUIDField(allow_null=True)
    approver_position_id = serializers.UUIDField(allow_null=True)
    approver_pool_size = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalRule
        fields = [
            "id",
            "name",
            "active",
            "priority",
            "category_id",
            "currency",
            "threshold_minor",
            *CRITERIA_FIELDS,
            "approver_pool_size",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_threshold_minor(self, obj):
        return str(obj.threshold_minor)

    @extend_schema_field(OpenApiTypes.INT)
    def get_approver_pool_size(self, obj):
        return len(resolve_pool(obj))


class ApprovalRuleWriteSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    threshold_minor = StrictStringField(required=False)
    currency = StrictStringField(max_length=3)
    priority = serializers.IntegerField(min_value=0, required=False)
    category_id = serializers.PrimaryKeyRelatedField(
        source="category",
        queryset=ExpenseCategory.objects.filter(active=True),
        allow_null=True,
        required=False,
    )
    submitter_location_id = org_attribute_field(
        OrgAttribute.Dimension.LOCATION, "submitter_location"
    )
    submitter_department_id = org_attribute_field(
        OrgAttribute.Dimension.DEPARTMENT, "submitter_department"
    )
    submitter_position_id = org_attribute_field(
        OrgAttribute.Dimension.POSITION, "submitter_position"
    )
    approver_role = serializers.ChoiceField(
        choices=[User.Role.APPROVER, User.Role.ADMIN],
        allow_null=True,
        required=False,
    )
    approver_location_id = org_attribute_field(OrgAttribute.Dimension.LOCATION, "approver_location")
    approver_department_id = org_attribute_field(
        OrgAttribute.Dimension.DEPARTMENT, "approver_department"
    )
    approver_position_id = org_attribute_field(OrgAttribute.Dimension.POSITION, "approver_position")

    class Meta:
        model = ApprovalRule
        fields = [
            "name",
            "active",
            "priority",
            "category_id",
            "currency",
            "threshold_minor",
            *CRITERIA_FIELDS,
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

    def _save_validated(self, instance):
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance

    def create(self, validated_data):
        return self._save_validated(ApprovalRule(**validated_data))

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        return self._save_validated(instance)


class ApprovalRuleViewSet(viewsets.ModelViewSet):
    queryset = ApprovalRule.objects.select_related(
        "category",
        "submitter_location",
        "submitter_department",
        "submitter_position",
        "approver_location",
        "approver_department",
        "approver_position",
    ).order_by("priority", "name", "id")
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        return [IsAuthenticated()] if self.action in {"list", "retrieve"} else [IsAdmin()]

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.role != User.Role.ADMIN:
            queryset = queryset.filter(active=True)
        return queryset

    def get_serializer_class(self):
        return (
            ApprovalRuleReadSerializer
            if self.action in {"list", "retrieve"}
            else ApprovalRuleWriteSerializer
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        return Response(ApprovalRuleReadSerializer(rule).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        rule = serializer.save()
        return Response(ApprovalRuleReadSerializer(rule).data)
