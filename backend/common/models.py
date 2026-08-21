from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def foreign_key_changed(instance: models.Model, field_name: str) -> bool:
    if instance._state.adding:
        return True
    previous = (
        type(instance)
        .objects.filter(pk=instance.pk)
        .values_list(f"{field_name}_id", flat=True)
        .first()
    )
    return previous != getattr(instance, f"{field_name}_id")
