from django import forms
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.contrib.auth.password_validation import validate_password

from users.models import User


class UserCreationForm(forms.ModelForm):
    password1 = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(
        label="Password confirmation",
        strip=False,
        widget=forms.PasswordInput,
    )

    class Meta:
        model = User
        fields = ("email", "full_name", "role", "is_active", "is_staff")

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("The two password fields did not match.")
        if password2:
            validate_password(password2, self.instance)
        return password2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
            self.save_m2m()
        return user


class UserChangeForm(forms.ModelForm):
    password = ReadOnlyPasswordHashField(
        label="Password",
        help_text=(
            "Passwords are not stored in plain text, so this value cannot be viewed. "
            "Use the admin password-change form to set a new password."
        ),
    )

    class Meta:
        model = User
        fields = (
            "email",
            "password",
            "full_name",
            "role",
            "location",
            "department",
            "position",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
        )

    def clean_password(self):
        return self.initial["password"]
