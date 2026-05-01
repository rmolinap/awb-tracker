from app.services.normalize import normalize_awb, normalize_carrier


def test_normalize_carrier_aliases() -> None:
    assert normalize_carrier("Delta") == "delta"
    assert normalize_carrier("delta") == "delta"
    assert normalize_carrier("Delta Cargo") == "delta"
    assert normalize_carrier("United") == "united"
    assert normalize_carrier("United Cargo") == "united"
    assert normalize_carrier("Southwest") == "southwest"
    assert normalize_carrier("SWA") == "southwest"
    assert normalize_carrier("Southwest Cargo") == "southwest"
    assert normalize_carrier("American") == "american"
    assert normalize_carrier("AA") == "american"
    assert normalize_carrier("American Airlines Cargo") == "american"
    assert normalize_carrier("Alaska") == "alaska"
    assert normalize_carrier("Alaska Cargo") == "alaska"


def test_normalize_awb_removes_dashes_and_spaces() -> None:
    assert normalize_awb("006-22953556") == "00622953556"
    assert normalize_awb("006 2295 3556") == "00622953556"
    assert normalize_awb(" 006-2295 3556 ") == "00622953556"
