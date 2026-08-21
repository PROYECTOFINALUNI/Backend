import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from common.models import TimeStampedModel, foreign_key_changed


class OrgAttribute(TimeStampedModel):
    """A value in one of the three organisational dimensions used by approval rules."""

    class Dimension(models.TextChoices):
        LOCATION = "LOCATION", "Location"
        DEPARTMENT = "DEPARTMENT", "Department"
        POSITION = "POSITION", "Position"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dimension = models.CharField(max_length=16, choices=Dimension.choices)
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["dimension", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["dimension", "code"],
                name="org_attribute_dimension_code_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["dimension", "active"], name="org_attribute_lookup_idx"),
        ]

    def clean(self):
        super().clean()
        self.code = self.code.strip().upper()
        if not self.code:
            raise ValidationError({"code": "Attribute code cannot be empty."})

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.dimension} — {self.name}"


def org_attribute_errors(instance, *, fields, require_active=True):
    """Validate that each named FK points at an attribute of the expected dimension.

    ``fields`` maps a model field name to the ``OrgAttribute.Dimension`` it must belong to.
    Inactive values are only rejected when the field is newly set, so deactivating a value
    never makes existing rows unsaveable.
    """
    errors = {}
    for field_name, dimension in fields.items():
        attribute_id = getattr(instance, f"{field_name}_id")
        if not attribute_id:
            continue
        cached = instance._state.fields_cache.get(field_name)
        if cached is not None:
            actual, active = cached.dimension, cached.active
        else:
            row = OrgAttribute.objects.filter(pk=attribute_id).values("dimension", "active").first()
            if row is None:
                continue
            actual, active = row["dimension"], row["active"]
        label = OrgAttribute.Dimension(dimension).label.lower()
        if actual != dimension:
            errors[field_name] = f"Expected a {label} value."
        elif require_active and not active and foreign_key_changed(instance, field_name):
            errors[field_name] = f"A new {label} selection must be active."
    return errors


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("An email address is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.full_clean()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("role", User.Role.ADMIN)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        if extra_fields.get("role") != User.Role.ADMIN:
            raise ValueError("Superuser must have the admin role")
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    class Role(models.TextChoices):
        EMPLOYEE = "EMPLOYEE", "Employee"
        APPROVER = "APPROVER", "Approver"
        ADMIN = "ADMIN", "Admin"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.EMPLOYEE)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    location = models.ForeignKey(
        OrgAttribute,
        on_delete=models.PROTECT,
        related_name="users_by_location",
        null=True,
        blank=True,
    )
    department = models.ForeignKey(
        OrgAttribute,
        on_delete=models.PROTECT,
        related_name="users_by_department",
        null=True,
        blank=True,
    )
    position = models.ForeignKey(
        OrgAttribute,
        on_delete=models.PROTECT,
        related_name="users_by_position",
        null=True,
        blank=True,
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "app_user"
        constraints = [
            models.UniqueConstraint(Lower("email"), name="users_email_ci_unique"),
        ]
        indexes = [
            models.Index(fields=["role", "is_active"], name="users_role_active_idx"),
            models.Index(
                fields=["location", "department", "position"],
                name="users_org_attributes_idx",
            ),
        ]

    ORG_ATTRIBUTE_FIELDS = {
        "location": OrgAttribute.Dimension.LOCATION,
        "department": OrgAttribute.Dimension.DEPARTMENT,
        "position": OrgAttribute.Dimension.POSITION,
    }

    def clean(self):
        super().clean()
        self.email = self.__class__.objects.normalize_email(self.email).lower()
        errors = org_attribute_errors(self, fields=self.ORG_ATTRIBUTE_FIELDS)
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.email
