"""Make and model normalisation for SDRs (app/ingest/sdr_models.py). The raw designations are real ones from
the 2024-2026 files; coverage on all of them is measured by scripts/measure_sdr_models.py (docs/sdr.md)."""

import pytest

from app.ingest.sdr_models import normalise_make, normalise_model, normalise_query_model


@pytest.mark.parametrize(
    ("make", "model", "expected"),
    [
        ("BOEING", "7378H4", ("737-800", "737")),  # customer code H4 (Southwest)
        ("BOEING", "737823", ("737-800", "737")),
        ("BOEING", "7377H4", ("737-700", "737")),
        ("BOEING", "737924ER", ("737-900ER", "737")),
        ("BOEING", "7378", ("737-8", "737")),  # no customer code: the 737 MAX 8, not a 737-800
        ("BOEING", "7379", ("737-9", "737")),
        ("BOEING", "7879", ("787-9", "787")),
        ("BOEING", "78710", ("787-10", "787")),
        ("BOEING", "75724APF", ("757-200F", "757")),
        ("BOEING", "76734AF", ("767-300F", "767")),
        ("BOEING", "777FS2", ("777F", "777")),
        ("BOEING", "717200", ("717-200", "717")),
        ("AIRBUS", "A321253NX", ("A321-253NX", "A321")),
        ("AIRBUS", "A320214", ("A320-214", "A320")),
        ("AIRBUS", "A330941", ("A330-941", "A330")),
        ("AIRBUS", "A300F4622R", ("A300F4-622R", "A300")),
        ("AIRBUS", "A300B4622R", ("A300-600R", "A300")),
        ("AIRBUS", "BD5001A11", ("A220-300", "A220")),  # the A220 keeps its Bombardier type designation
        ("CNDAIR", "CL6002D24", ("CRJ900", "CRJ")),
        ("CNDAIR", "CL6002C10", ("CRJ700", "CRJ")),
        ("BOMBDR", "CL6002B19", ("CRJ200", "CRJ")),
        ("BOMBDR", "BD1001A10", ("Challenger 300", "Challenger")),
        ("BOMBDR", "DHC8402", ("Dash 8-400", "Dash 8")),
        ("EMB", "ERJ170200LR", ("E175", "E-Jet")),
        ("EMB", "ERJ190100", ("E190", "E-Jet")),
        ("EMB", "EMB145XR", ("ERJ145", "ERJ")),
        ("EMB", "EMB505", ("Phenom 300", "Phenom")),
        ("DOUG", "MD11F", ("MD-11F", "MD-11")),
        ("DOUG", "MD88", ("MD-88", "MD-80")),
        ("DOUG", "DC983", ("DC-9", "DC-9")),  # DC-9-83, not a "DC-98"
        ("DOUG", "DC1030F", ("DC-10", "DC-10")),
        ("AEROSP", "ATR72212A", ("ATR 72", "ATR 72")),
    ],
)
def test_real_designations_map_to_the_names_people_use(make: str, model: str, expected: tuple[str, str]) -> None:
    assert normalise_model(make, model) == expected


def test_unknown_designations_keep_the_raw_model_and_get_no_family() -> None:
    assert normalise_model("CESSNA", " 680A ") == ("680A", None)
    assert normalise_model("BOEING", "") == ("", None)
    assert normalise_model("BOEING", "VC25B") == ("VC25B", None)


def test_makes_are_expanded_and_unknown_makes_title_cased() -> None:
    assert [normalise_make(m) for m in ("CNDAIR", "BOMBDR", "EMB", "AMD", "ROBSIN", "ZLIN", "")] == [
        "Bombardier", "Bombardier", "Embraer", "Dassault", "Robinson", "Zlin", ""]  # fmt: skip


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("737-800", "737-800"),
        ("Boeing 737-800", "737-800"),  # the fleet table's type
        ("737", "737"),  # a family stays a family, not the 737-7
        ("737 MAX 8", "737-8"),
        ("737-8", "737-8"),
        ("Bombardier CRJ900 (CL-600-2D24)", "CRJ900"),
        ("CL-600-2D24", "CRJ900"),
        ("CRJ900", "CRJ900"),
        ("Airbus A320-214", "A320-214"),
        ("A320", "A320"),
        ("A320neo", "A320"),
        ("E175", "E175"),
        ("A220-300", "A220-300"),
        ("MD-11", "MD-11"),
    ],
)
def test_query_terms_normalise_like_the_data(term: str, expected: str) -> None:
    assert normalise_query_model(term) == expected
