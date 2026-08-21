"""Validación y almacenamiento de los campos personalizados asociados a una categoría.
"""

from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

from expenses.models import CategoryField, ExpenseFieldValue

MAX_TEXT_LENGTH = 500
# Límites definidos para los campos numéricos personalizados.
NUMBER_DECIMAL_PLACES = 4
MAX_NUMBER_DIGITS = 18


def _error(field_id, message):
    return ValidationError({f"custom_fields.{field_id}": message})


def _coerce_text(field, value):
    if not isinstance(value, str):
        raise _error(field.id, f"'{field.name}' expects text.")
    value = value.strip()
    if len(value) > MAX_TEXT_LENGTH:
        raise _error(field.id, f"'{field.name}' cannot exceed {MAX_TEXT_LENGTH} characters.")
    return value


def _coerce_number(field, value):
    # Los booleanos también son considerados enteros en Python, por lo que se excluyen.
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise _error(field.id, f"'{field.name}' expects a number.")
    try:
        # Se convierte primero a texto para evitar imprecisiones de los números flotantes.
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise _error(field.id, f"'{field.name}' expects a number.") from exc
    if not number.is_finite():
        raise _error(field.id, f"'{field.name}' expects a number.")
    if -number.as_tuple().exponent > NUMBER_DECIMAL_PLACES:
        raise _error(
            field.id,
            f"'{field.name}' allows at most {NUMBER_DECIMAL_PLACES} decimal places.",
        )
    if len(number.as_tuple().digits) > MAX_NUMBER_DIGITS:
        raise _error(field.id, f"'{field.name}' is too large.")
    return number


def _coerce_boolean(field, value):
    if not isinstance(value, bool):
        raise _error(field.id, f"'{field.name}' expects true or false.")
    return value


_COERCERS = {
    CategoryField.FieldType.TEXT: _coerce_text,
    CategoryField.FieldType.NUMBER: _coerce_number,
    CategoryField.FieldType.BOOLEAN: _coerce_boolean,
}


def _is_unset(value):
   # Un valor False es válido; únicamente None o un texto vacío se consideran sin rellenar.
    return value is None or (isinstance(value, str) and not value.strip())


def active_definitions(category_id):
    if not category_id:
        return []
    return list(CategoryField.objects.filter(category_id=category_id, active=True))


def _as_mapping(items):
    submitted = {}
    for item in items:
        field_id = str(item["field_id"])
        if field_id in submitted:
            raise _error(field_id, "This field was answered more than once.")
        submitted[field_id] = item["value"]
    return submitted


def replace_field_values(expense, items=None):
    """Valida y sustituye los valores personalizados asociados a un gasto.
    """
    definitions = active_definitions(expense.category_id)
    stored = {row.field_id: row for row in expense.field_values.select_related("field")}

    if items is None:
        submitted = {}
        for field in definitions:
            row = stored.get(field.id)
            if row is not None:
                column = ExpenseFieldValue.VALUE_COLUMNS[field.field_type]
                submitted[str(field.id)] = getattr(row, column)
    else:
        submitted = _as_mapping(items)

    known = {str(field.id): field for field in definitions}
    for field_id in submitted:
        if field_id not in known:
            raise _error(field_id, "This field does not belong to the selected category.")

    keep_ids = set()
    for field in definitions:
        raw = submitted.get(str(field.id))
        if _is_unset(raw):
            if field.required:
                raise _error(field.id, f"'{field.name}' is required.")
            continue
        value = _COERCERS[field.field_type](field, raw)
        row = stored.get(field.id) or ExpenseFieldValue(expense=expense, field=field)
        for column in ExpenseFieldValue.VALUE_COLUMNS.values():
            setattr(row, column, "" if column == "value_text" else None)
        setattr(row, ExpenseFieldValue.VALUE_COLUMNS[field.field_type], value)
        row.full_clean(exclude=["expense", "field"])
        row.save()
        keep_ids.add(field.id)

# Se conservan los valores históricos de campos desactivados.
    inactive_on_this_category = {
        field_id
        for field_id, row in stored.items()
        if row.field.category_id == expense.category_id and not row.field.active
    }
    obsolete = set(stored) - keep_ids - inactive_on_this_category
    if obsolete:
        ExpenseFieldValue.objects.filter(expense=expense, field_id__in=obsolete).delete()


def _readable(value):
    if not isinstance(value, Decimal):
        return value
# Los valores decimales se devuelven como texto para mantener su precisión.
    normalized = value.normalize()
    if normalized.as_tuple().exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return str(normalized)


def read_values(expense):
    """Devuelve los campos personalizados almacenados en su orden de visualización."""
    return [
        {
            "field_id": row.field_id,
            "name": row.field.name,
            "field_type": row.field.field_type,
            "value": _readable(row.value),
        }
        for row in expense.field_values.all()
    ]
