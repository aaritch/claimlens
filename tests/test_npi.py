from claimlens.rules.npi import is_valid_npi, luhn_valid, make_npi


def test_known_valid_npi():
    assert is_valid_npi("1234567893")  # the standard example NPI


def test_one_wrong_digit_is_caught():
    assert not is_valid_npi("1234567894")


def test_bad_shapes():
    for bad in ["", "123", "12345678901", "abcdefghij", "123456789 "]:
        assert not is_valid_npi(bad)


def test_make_npi_always_valid():
    for first_nine in ["100000000", "234567891", "999999999"]:
        assert is_valid_npi(make_npi(first_nine))


def test_luhn_basic():
    assert luhn_valid("79927398713")
    assert not luhn_valid("79927398710")
