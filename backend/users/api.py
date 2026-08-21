from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from common.api import DomainConflict, StrictFieldsMixin, StrictSerializer
from users.models import OrgAttribute, User


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.role == User.Role.ADMIN)


class IsApproverOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user.is_authenticated
            and request.user.role in {User.Role.APPROVER, User.Role.ADMIN}
        )


def _is_admin(user):
    return user.role == User.Role.ADMIN


class OrgAttributeReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrgAttribute
        fields = ["id", "dimension", "code", "name", "active", "created_at", "updated_at"]


class OrgAttributeWriteSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = OrgAttribute
        fields = ["dimension", "code", "name", "active"]

    def validate_code(self, value):
        return value.strip().upper()

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
        return self._save_validated(OrgAttribute(**validated_data))

    def update(self, instance, validated_data):
        if "dimension" in validated_data and validated_data["dimension"] != instance.dimension:
            raise serializers.ValidationError({"dimension": "Dimension is immutable."})
        for field, value in validated_data.items():
            setattr(instance, field, value)
        return self._save_validated(instance)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                "dimension",
                OpenApiTypes.STR,
                enum=OrgAttribute.Dimension.values,
                description="Limit to a single organisational dimension.",
            ),
            OpenApiParameter("active", OpenApiTypes.BOOL),
        ]
    )
)
class OrgAttributeViewSet(viewsets.ModelViewSet):
    queryset = OrgAttribute.objects.all()
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        return [IsAuthenticated()] if self.action in {"list", "retrieve"} else [IsAdmin()]

    def get_queryset(self):
        queryset = super().get_queryset()
        if not _is_admin(self.request.user):
            queryset = queryset.filter(active=True)
        dimension = self.request.query_params.get("dimension")
        if dimension:
            queryset = queryset.filter(dimension=dimension)
        active = self.request.query_params.get("active")
        if active is not None:
            queryset = queryset.filter(active=active.lower() == "true")
        return queryset

    def get_serializer_class(self):
        return (
            OrgAttributeReadSerializer
            if self.action in {"list", "retrieve"}
            else OrgAttributeWriteSerializer
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attribute = serializer.save()
        return Response(OrgAttributeReadSerializer(attribute).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        attribute = serializer.save()
        return Response(OrgAttributeReadSerializer(attribute).data)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise DomainConflict(
                "org_attribute_in_use",
                "Attributes referenced by users or approval rules cannot be deleted.",
            ) from exc


class UserReadSerializer(serializers.ModelSerializer):
    active = serializers.BooleanField(source="is_active")
    location = OrgAttributeReadSerializer(allow_null=True)
    department = OrgAttributeReadSerializer(allow_null=True)
    position = OrgAttributeReadSerializer(allow_null=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "role",
            "active",
            "location",
            "department",
            "position",
            "created_at",
            "updated_at",
        ]


def org_attribute_field(dimension, source):
    """A writable ``<dimension>Id`` field restricted to active values of that dimension."""
    return serializers.PrimaryKeyRelatedField(
        source=source,
        queryset=OrgAttribute.objects.filter(dimension=dimension, active=True),
        allow_null=True,
        required=False,
    )


ORG_ATTRIBUTE_WRITE_FIELDS = ["location_id", "department_id", "position_id"]


class UserWriteSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    active = serializers.BooleanField(source="is_active", required=False)
    password = serializers.CharField(write_only=True)
    location_id = org_attribute_field(OrgAttribute.Dimension.LOCATION, "location")
    department_id = org_attribute_field(OrgAttribute.Dimension.DEPARTMENT, "department")
    position_id = org_attribute_field(OrgAttribute.Dimension.POSITION, "position")

    class Meta:
        model = User
        fields = ["email", "full_name", "role", "active", "password", *ORG_ATTRIBUTE_WRITE_FIELDS]

    def validate_password(self, value):
        candidate = User(
            email=self.initial_data.get("email", ""),
            full_name=self.initial_data.get("full_name", ""),
        )
        try:
            validate_password(value, candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        return User.objects.create_user(password=password, **validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password is not None:
            instance.set_password(password)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance


class UserPatchSerializer(StrictFieldsMixin, serializers.ModelSerializer):
    active = serializers.BooleanField(source="is_active", required=False)
    password = serializers.CharField(write_only=True, required=False)
    location_id = org_attribute_field(OrgAttribute.Dimension.LOCATION, "location")
    department_id = org_attribute_field(OrgAttribute.Dimension.DEPARTMENT, "department")
    position_id = org_attribute_field(OrgAttribute.Dimension.POSITION, "position")

    class Meta:
        model = User
        fields = ["email", "full_name", "role", "active", "password", *ORG_ATTRIBUTE_WRITE_FIELDS]

    def validate_password(self, value):
        try:
            validate_password(value, self.instance)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password is not None:
            instance.set_password(password)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance


class LoginSerializer(StrictSerializer):
    email = serializers.EmailField()
    password = serializers.CharField()


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserReadSerializer()


class LogoutSerializer(StrictSerializer):
    refresh = serializers.CharField(required=False, allow_blank=True)


class PasswordSerializer(StrictSerializer):
    password = serializers.CharField()

    def validate_password(self, value):
        try:
            validate_password(value, self.context.get("user"))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


@extend_schema(request=LoginSerializer, responses=TokenPairSerializer)
@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"].lower()
    user = authenticate(request, username=email, password=serializer.validated_data["password"])
    if user is None or not user.is_active:
        return Response(
            {"code": "invalid_credentials", "detail": "Invalid credentials.", "fields": {}},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    refresh = RefreshToken.for_user(user)
    return Response(
        {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserReadSerializer(user).data,
        }
    )


@extend_schema(request=LogoutSerializer, responses={205: None})
@api_view(["POST"])
@permission_classes([AllowAny])
def logout_view(request):
    serializer = LogoutSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    token = serializer.validated_data.get("refresh")
    if token:
        try:
            RefreshToken(token).blacklist()
        except TokenError:
            pass
    return Response(status=status.HTTP_205_RESET_CONTENT)


@extend_schema(responses=UserReadSerializer)
@api_view(["GET"])
def me(request):
    return Response(UserReadSerializer(request.user).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter("role", OpenApiTypes.STR, enum=User.Role.values),
            OpenApiParameter("active", OpenApiTypes.BOOL),
            OpenApiParameter("search", OpenApiTypes.STR),
            OpenApiParameter("location", OpenApiTypes.UUID),
            OpenApiParameter("department", OpenApiTypes.UUID),
            OpenApiParameter("position", OpenApiTypes.UUID),
        ]
    )
)
class UserViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = User.objects.select_related("location", "department", "position").order_by("email")

    APPROVER_ROLES = [User.Role.APPROVER, User.Role.ADMIN]

    def get_permissions(self):
        # El aprobador necesita nombrar a un compañero cuando vaya a delegar
        if self.action == "list":
            return [IsApproverOrAdmin()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action in {"list", "retrieve"}:
            return UserReadSerializer
        if self.action == "partial_update":
            return UserPatchSerializer
        return UserWriteSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if not _is_admin(self.request.user):
            queryset = queryset.filter(is_active=True, role__in=self.APPROVER_ROLES)
        params = self.request.query_params
        role = params.get("role")
        active = params.get("active")
        search = params.get("search")
        if role:
            queryset = queryset.filter(role=role)
        if active is not None:
            queryset = queryset.filter(is_active=active.lower() == "true")
        if search:
            queryset = queryset.filter(Q(email__icontains=search) | Q(full_name__icontains=search))
        for parameter in ("location", "department", "position"):
            if params.get(parameter):
                queryset = queryset.filter(**{f"{parameter}_id": params[parameter]})
        return queryset

    def create(self, request, *args, **kwargs):
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        user = write.save()
        return Response(UserReadSerializer(user).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        user = self.get_object()
        write = self.get_serializer(user, data=request.data, partial=True)
        write.is_valid(raise_exception=True)
        user = write.save()
        return Response(UserReadSerializer(user).data)

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        user = self.get_object()
        serializer = PasswordSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        user.set_password(serializer.validated_data["password"])
        user.save(update_fields=["password"])
        return Response(status=status.HTTP_204_NO_CONTENT)
