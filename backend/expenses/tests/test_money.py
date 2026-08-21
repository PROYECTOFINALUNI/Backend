import pytest

from expenses.services.money import (
    BIGINT_MAX,
    MoneyDTO,
    TaxCalculation,
    calculate_tax,
    calculate_tax_excluded,
    calculate_tax_included,
    check_minor_amount,
    display_money,
    minor_to_decimal_string,
    parse_decimal_string,
    round_half_up_positive,
    validate_currency,
)


@pytest.mark.parametrize(
    ("value", "currency", "minor"),
    [
        ("0", "EUR", 0),
        ("1", "EUR", 100),
        ("1.2", "USD", 120),
        ("1.23", "GBP", 123),
        ("0001.05", "EUR", 105),
        ("999999.99", "USD", 99_999_999),
        ("0", "JPY", 0),
        ("42", "JPY", 42),
    ],
)
def test_decimal_string_conversions(value, currency, minor):
    assert parse_decimal_string(value, currency) == minor


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [
        (0, "EUR", "0.00"),
        (1, "EUR", "0.01"),
        (120, "USD", "1.20"),
        (123, "GBP", "1.23"),
        (42, "JPY", "42"),
    ],
)
def test_minor_to_decimal_conversions(minor, currency, expected):
    assert minor_to_decimal_string(minor, currency) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "-1",
        "+1",
        ".50",
        "1.",
        "1.234",
        "1,00",
        "1e2",
        "NaN",
        "Infinity",
        "$1.00",
        " 1.00",
        "1.00 ",
    ],
)
def test_invalid_decimal_formats(value):
    with pytest.raises(ValueError, match="Invalid decimal money string"):
        parse_decimal_string(value, "EUR")


@pytest.mark.parametrize("value", [None, 1, 1.0, True, [], {}])
def test_decimal_input_must_be_string(value):
    with pytest.raises(TypeError, match="decimal string"):
        parse_decimal_string(value, "EUR")


@pytest.mark.parametrize("currency", ["CAD", "", "EU", "EURO", "€", None, 123])
def test_invalid_currency(currency):
    exception = TypeError if not isinstance(currency, str) else ValueError
    with pytest.raises(exception):
        validate_currency(currency)


def test_currency_is_normalized():
    assert validate_currency(" eur ") == "EUR"


@pytest.mark.parametrize("value", [-1, BIGINT_MAX + 1])
def test_minor_amount_outside_bigint_range(value):
    with pytest.raises(OverflowError):
        check_minor_amount(value)


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_minor_amount_rejects_non_integer_and_bool(value):
    with pytest.raises(TypeError):
        check_minor_amount(value)


def test_decimal_conversion_detects_bigint_overflow():
    with pytest.raises(OverflowError):
        parse_decimal_string("92233720368547758.08", "EUR")


@pytest.mark.parametrize(
    ("net_minor", "rate_bps", "tax_minor", "total_minor"),
    [
        (10_000, 2100, 2100, 12_100),
        (9_999, 2100, 2100, 12_099),
        (100, 1000, 10, 110),
        (1, 5000, 1, 2),
        (1, 4999, 0, 1),
        (0, 10_000, 0, 0),
    ],
)
def test_tax_excluded_examples(net_minor, rate_bps, tax_minor, total_minor):
    assert calculate_tax_excluded(net_minor, rate_bps) == TaxCalculation(
        net_minor, tax_minor, total_minor
    )


def test_tax_included_example():
    assert calculate_tax_included(12_100, 2100) == TaxCalculation(
        net_minor=10_000,
        tax_minor=2_100,
        total_minor=12_100,
    )


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [(4, 10, 0), (5, 10, 1), (6, 10, 1), (14, 10, 1), (15, 10, 2)],
)
def test_round_half_up_boundaries(numerator, denominator, expected):
    assert round_half_up_positive(numerator, denominator) == expected


@pytest.mark.parametrize(
    ("numerator", "denominator", "exception"),
    [(-1, 2, ValueError), (1, 0, ValueError), (True, 2, TypeError), (1, 2.0, TypeError)],
)
def test_round_half_up_rejects_invalid_inputs(numerator, denominator, exception):
    with pytest.raises(exception):
        round_half_up_positive(numerator, denominator)


@pytest.mark.parametrize("rate", [-1, 10_001])
def test_tax_rate_out_of_range(rate):
    with pytest.raises(ValueError):
        calculate_tax_excluded(100, rate)


@pytest.mark.parametrize("rate", [True, 1.0, "100"])
def test_tax_rate_must_be_integer(rate):
    with pytest.raises(TypeError):
        calculate_tax_excluded(100, rate)


def test_tax_excluded_total_overflow():
    with pytest.raises(OverflowError):
        calculate_tax_excluded(BIGINT_MAX, 1)


def test_calculate_tax_requires_boolean_included_flag():
    with pytest.raises(TypeError):
        calculate_tax(100, 2100, tax_included=1)


def test_money_dto_uses_wire_format_strings():
    assert MoneyDTO(123, "eur").as_dict() == {
        "currency": "EUR",
        "minor": "123",
        "decimal": "1.23",
        "display": "EUR 1.23",
    }
    assert display_money(42, "JPY") == "JPY 42"
