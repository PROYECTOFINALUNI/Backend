import re
from dataclasses import dataclass
from types import MappingProxyType

BIGINT_MAX = 9_223_372_036_854_775_807
# Número de decimales utilizados por cada moneda
CURRENCY_MINOR_UNITS = MappingProxyType({"EUR": 2, "USD": 2, "GBP": 2, "JPY": 0})


def validate_currency(currency: str) -> str:
    """Valida y normaliza el código de moneda recibido."""
    if not isinstance(currency, str):
        raise TypeError("Currency must be a string.")
    normalized = currency.strip().upper()
    if normalized not in CURRENCY_MINOR_UNITS:
        raise ValueError(f"Unsupported currency: {currency!r}")
    return normalized


def _minor_units(currency: str) -> tuple[str, int]:
    normalized = validate_currency(currency)
    return normalized, CURRENCY_MINOR_UNITS[normalized]


def check_minor_amount(amount_minor: int) -> int:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("Minor-unit amount must be an integer.")
    if not 0 <= amount_minor <= BIGINT_MAX:
        raise OverflowError("Minor-unit amount is outside the non-negative bigint range.")
    return amount_minor


def parse_decimal_string(value: str, currency: str) -> int:
    """Convierte un importe decimal en unidades monetarias mínimas"""
    _, minor_units = _minor_units(currency)
    if not isinstance(value, str):
        raise TypeError("Money input must be a decimal string.")

    pattern = (
        r"(?:0|[0-9]+)" if minor_units == 0 else rf"(?:0|[0-9]+)(?:\.[0-9]{{1,{minor_units}}})?"
    )
    if re.fullmatch(pattern, value) is None:
        raise ValueError("Invalid decimal money string.")

    whole, separator, fraction = value.partition(".")
    padded_fraction = fraction.ljust(minor_units, "0") if separator else "0" * minor_units
    # Los importes se almacenan como enteros para evitar errores de precisión con decimales.
    amount_minor = int(whole) * (10**minor_units) + int(padded_fraction or "0")
    return check_minor_amount(amount_minor)


decimal_to_minor = parse_decimal_string


def minor_to_decimal_string(amount_minor: int, currency: str) -> str:
    _, minor_units = _minor_units(currency)
    check_minor_amount(amount_minor)
    if minor_units == 0:
        return str(amount_minor)
    scale = 10**minor_units
    whole, fraction = divmod(amount_minor, scale)
    return f"{whole}.{fraction:0{minor_units}d}"


minor_to_decimal = minor_to_decimal_string


def display_money(amount_minor: int, currency: str) -> str:
    normalized = validate_currency(currency)
    return f"{normalized} {minor_to_decimal_string(amount_minor, normalized)}"


format_money = display_money


@dataclass(frozen=True, slots=True)
class MoneyDTO:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        check_minor_amount(self.amount_minor)
        object.__setattr__(self, "currency", validate_currency(self.currency))

    @property
    def minor(self) -> str:
        return str(self.amount_minor)

    @property
    def decimal(self) -> str:
        return minor_to_decimal_string(self.amount_minor, self.currency)

    @property
    def display(self) -> str:
        return display_money(self.amount_minor, self.currency)

    def as_dict(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "minor": self.minor,
            "decimal": self.decimal,
            "display": self.display,
        }


def create_money_dto(amount_minor: int, currency: str) -> MoneyDTO:
    return MoneyDTO(amount_minor=amount_minor, currency=currency)


@dataclass(frozen=True, slots=True)
class TaxCalculation:
    net_minor: int
    tax_minor: int
    total_minor: int

    def __post_init__(self) -> None:
        check_minor_amount(self.net_minor)
        check_minor_amount(self.tax_minor)
        check_minor_amount(self.total_minor)


def round_half_up_positive(numerator: int, denominator: int) -> int:
    """Redondea al entero más cercano, redondeando hacia arriba en caso de empate."""
    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or not isinstance(numerator, int)
        or not isinstance(denominator, int)
    ):
        raise TypeError("Numerator and denominator must be integers.")
    if numerator < 0 or denominator <= 0:
        raise ValueError("Expected a non-negative numerator and positive denominator.")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + (1 if remainder * 2 >= denominator else 0)


round_half_up = round_half_up_positive

# El tipo impositivo se expresa en puntos básicos: 2100 equivale al 21 %.
def _check_tax_rate(tax_rate_bps: int) -> int:
    if isinstance(tax_rate_bps, bool) or not isinstance(tax_rate_bps, int):
        raise TypeError("Tax rate basis points must be an integer.")
    if not 0 <= tax_rate_bps <= 10_000:
        raise ValueError("Tax rate basis points must be between 0 and 10000.")
    return tax_rate_bps

# El importe recibido corresponde a la base imponible.
def calculate_tax_excluded(net_minor: int, tax_rate_bps: int) -> TaxCalculation:
    check_minor_amount(net_minor)
    _check_tax_rate(tax_rate_bps)
    tax_minor = round_half_up_positive(net_minor * tax_rate_bps, 10_000)
    total_minor = net_minor + tax_minor
    check_minor_amount(total_minor)
    return TaxCalculation(net_minor=net_minor, tax_minor=tax_minor, total_minor=total_minor)

# El importe recibido ya contiene el impuesto.
def calculate_tax_included(total_minor: int, tax_rate_bps: int) -> TaxCalculation:
    check_minor_amount(total_minor)
    _check_tax_rate(tax_rate_bps)
    tax_minor = round_half_up_positive(total_minor * tax_rate_bps, 10_000 + tax_rate_bps)
    return TaxCalculation(
        net_minor=total_minor - tax_minor,
        tax_minor=tax_minor,
        total_minor=total_minor,
    )


def calculate_tax(amount_minor: int, tax_rate_bps: int, *, tax_included: bool) -> TaxCalculation:
    if not isinstance(tax_included, bool):
        raise TypeError("tax_included must be a boolean.")
    if tax_included:
        return calculate_tax_included(amount_minor, tax_rate_bps)
    return calculate_tax_excluded(amount_minor, tax_rate_bps)
