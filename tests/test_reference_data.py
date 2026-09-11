import json

import reference_data


def test_normalise_payment_method_sequence_stores_code_before_name():
    row = [1, "SAGEPAY", "Sagepay Direct", True, "GBP", "1220"]

    normalised = reference_data._normalise_payment_method(row)

    assert normalised[:3] == (1, "SAGEPAY", "Sagepay Direct")
    assert json.loads(normalised[3]) == row


def test_normalise_payment_method_mapping_stores_code_before_name():
    row = {
        "paymentMethodId": 13,
        "code": "PAYPAL USD",
        "name": "PayPal USD",
    }

    normalised = reference_data._normalise_payment_method(row)

    assert normalised[:3] == (13, "PAYPAL USD", "PayPal USD")
    assert json.loads(normalised[3]) == row
