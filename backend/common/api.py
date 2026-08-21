from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotAuthenticated, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import exception_handler as drf_exception_handler


class DomainConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "The operation conflicts with the current state."

    def __init__(self, code, detail):
        super().__init__(detail=detail, code=code)
        self.domain_code = code


class StrictFieldsMixin:
    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = set(data.keys()) - set(self.fields)
            if unknown:
                raise ValidationError(
                    {field: ["Unknown or forbidden field."] for field in sorted(unknown)}
                )
        return super().to_internal_value(data)


class StrictSerializer(StrictFieldsMixin, serializers.Serializer):
    pass


class StrictStringField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "pageSize"
    max_page_size = 100


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        if isinstance(exc, DomainConflict):
            code = exc.domain_code
        elif isinstance(exc, NotAuthenticated):
            code = "not_authenticated"
        else:
            code = getattr(exc, "default_code", "error")
        is_validation = isinstance(exc, ValidationError)
        fields = response.data if is_validation else {}
        detail = "Validation failed." if is_validation else str(response.data.get("detail", exc))
        response.data = {"code": code, "detail": detail, "fields": fields}
    return response
