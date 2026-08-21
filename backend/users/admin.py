from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from users.forms import UserChangeForm, UserCreationForm
from users.models import OrgAttribute, User


@admin.register(OrgAttribute)
class OrgAttributeAdmin(admin.ModelAdmin):
    list_display = ("dimension", "code", "name", "active")
    list_filter = ("dimension", "active")
    search_fields = ("code", "name")
    ordering = ("dimension", "name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationForm
    form = UserChangeForm
    model = User

    list_display = ("email", "full_name", "role", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("email", "full_name")
    ordering = ("email",)
    readonly_fields = ("last_login", "created_at", "updated_at")
    filter_horizontal = ("groups", "user_permissions")
    autocomplete_fields = ("location", "department", "position")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "role", "location", "department", "position")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "full_name",
                    "role",
                    "password1",
                    "password2",
                    "is_active",
                    "is_staff",
                ),
            },
        ),
    )
