"""Make and model normalisation for Service Difficulty Reports (DESIGN §5.1 `model_aliases`, docs/sdr.md).

SDRs carry the FAA registry's abbreviated make and the manufacturer's full model designation, often with a
customer code: `BOEING 7378H4` is a 737-800 (H4 = Southwest), `CNDAIR CL6002D24` is a CRJ900, and
`AIRBUS BD5001A10` is an A220-100. Questions name aircraft the way people do ("737-800", "CRJ900", "A320"), so
each report gets a canonical `make`, `model` and `model_family`. The raw values are kept.

The rules were derived from the 182,910 reports of 2024-2026 and measured there (scripts/measure_sdr_models.py).
One trap, checked against operators: a bare `7378`/`7379` is the 737-8/737-9 **MAX** (Southwest, American and
United file their MAX fleets that way), not a 737-800/900, whose models always carry a customer code (`7378H4`).
The same function normalises query terms, so ingest and search agree.
"""

import re

MAKES = {  # every code in the 2024-2026 files with more than ~10 reports; others are title-cased
    "BOEING": "Boeing", "AIRBUS": "Airbus", "CNDAIR": "Bombardier", "BOMBDR": "Bombardier", "EMB": "Embraer",
    "DOUG": "McDonnell Douglas", "CESSNA": "Cessna", "LKHEED": "Lockheed", "BOLKMS": "MBB", "TEXTRN": "Textron",
    "PIPER": "Piper", "BEECH": "Beechcraft", "UROCOP": "Airbus Helicopters", "BELL": "Bell",
    "AEROSP": "Aerospatiale", "PILATS": "Pilatus", "GULSTM": "Gulfstream", "LEAR": "Learjet", "CIRRUS": "Cirrus",
    "DIAMON": "Diamond", "SAAB": "Saab", "SKRSKY": "Sikorsky", "SNIAS": "Aerospatiale", "TECNAM": "Tecnam",
    "CASA": "CASA", "AGUSTA": "Agusta", "ROBSIN": "Robinson", "RAYTHN": "Raytheon", "AMD": "Dassault",
    "SWRNGN": "Swearingen", "DHAV": "De Havilland", "MOONEY": "Mooney", "FRCHLD": "Fairchild",
    "AIRTRC": "Air Tractor", "HUGHES": "Hughes", "BNORM": "Britten-Norman", "DORNER": "Dornier",
}  # fmt: skip

# Canadair / Bombardier type-certificate designations -> the names people use.
CL600 = {"2B19": ("CRJ200", "CRJ"), "2C10": ("CRJ700", "CRJ"), "2C11": ("CRJ550", "CRJ"),
         "2D15": ("CRJ705", "CRJ"), "2D24": ("CRJ900", "CRJ"), "2E25": ("CRJ1000", "CRJ"),
         "2B16": ("Challenger 601/604/605", "Challenger")}  # fmt: skip
EMBRAER = [(r"^ERJ170200", "E175", "E-Jet"), (r"^ERJ170100", "E170", "E-Jet"), (r"^ERJ190100", "E190", "E-Jet"),
           (r"^ERJ190200", "E195", "E-Jet"), (r"^EMB145", "ERJ145", "ERJ"), (r"^EMB135", "ERJ135", "ERJ"),
           (r"^EMB140", "ERJ140", "ERJ"), (r"^EMB505", "Phenom 300", "Phenom"), (r"^EMB500", "Phenom 100", "Phenom"),
           (r"^EMB545", "Praetor 500", "Legacy/Praetor"), (r"^EMB550", "Praetor 600", "Legacy/Praetor")]  # fmt: skip
BD = [(r"^BD5001A10", "A220-100", "A220"), (r"^BD5001A11", "A220-300", "A220"),
      (r"^BD1001A10", "Challenger 300", "Challenger"), (r"^BD7001A1[0-2]", "Global", "Global"),
      (r"^BD7002A12", "Global 7500", "Global"), (r"^DHC8402", "Dash 8-400", "Dash 8")]  # fmt: skip


def normalise_make(raw: str) -> str:
    raw = raw.strip().upper()
    return MAKES.get(raw, raw.title()) if raw else ""


def _boeing(m: str) -> tuple[str, str] | None:
    if m.startswith("717"):
        return "717-200", "717"
    if m.startswith("777F"):  # 777F, 777FS2, 777F1H: the 777 Freighter
        return "777F", "777"
    g = re.match(r"^(7[0-8]7)(\d)(.*)$", m)
    if not g:
        return None
    family, series, rest = g.groups()
    if rest == "" and family in ("737", "787"):
        # No customer code: the 737 MAX (737-8, 737-9) and the 787 are designated this way.
        return f"{family}-{series}", family
    if family == "787" and rest.startswith("0"):  # 78710 -> 787-10
        return "787-10", "787"
    suffix = "ER" if rest.endswith("ER") else ("F" if rest.endswith("F") else "")
    return f"{family}-{series}00{suffix}", family


def _airbus(m: str) -> tuple[str, str] | None:
    for pat, model, family in BD:
        if re.match(pat, m):
            return model, family
    if m.startswith("A300B4"):  # A300B4622R: the A300-600R
        return "A300-600R" if m.endswith("R") else "A300-600", "A300"
    g = re.match(r"^A(3\d\d)(F\d?)?(\d{3})([A-Z]*)$", m)
    if not g:
        return None
    family = f"A{g.group(1)}"
    return f"{family}{g.group(2) or ''}-{g.group(3)}{g.group(4)}", family


def normalise_model(make_raw: str, model_raw: str) -> tuple[str, str | None]:
    """(canonical model, family). Unknown designations keep their raw model and no family."""
    mk, m = make_raw.strip().upper(), re.sub(r"[\s\-]", "", model_raw.strip().upper())
    if not m:
        return "", None
    out: tuple[str, str] | None = None
    if mk == "BOEING":
        out = _boeing(m)
    elif mk == "AIRBUS":
        out = _airbus(m)
    elif mk in ("CNDAIR", "BOMBDR"):
        g = re.match(r"^CL600(\d[A-Z]\d{2})", m)
        if g and g.group(1) in CL600:
            out = CL600[g.group(1)]
        else:
            out = next(((mo, fa) for pat, mo, fa in BD if re.match(pat, m)), None)
    elif mk == "EMB":
        out = next(((mo, fa) for pat, mo, fa in EMBRAER if re.match(pat, m)), None)
    elif mk == "DOUG":
        if g := re.match(r"^MD(\d\d)(F?)", m):  # MD11F, MD83, MD88
            out = (f"MD-{g.group(1)}{g.group(2)}", "MD-80" if g.group(1).startswith("8") else f"MD-{g.group(1)}")
        elif g := re.match(r"^DC(10|\d)", m):  # DC983 is a DC-9-83, DC915F a DC-9-15F, DC6A a DC-6A
            out = (f"DC-{g.group(1)}", f"DC-{g.group(1)}")
    elif mk == "AEROSP":
        if g := re.match(r"^ATR(42|72)", m):
            out = (f"ATR {g.group(1)}", f"ATR {g.group(1)}")
    if out:
        return out
    return model_raw.strip(), None


# Words people use that aren't designations at all.
QUERY_ALIASES = {"737MAX8": "737-8", "737MAX9": "737-9", "737MAX": "737", "MAX8": "737-8", "MAX9": "737-9",
                 "CRJ9": "CRJ900", "CRJ7": "CRJ700", "CRJ2": "CRJ200", "E175": "E175", "ERJ175": "E175",
                 "A320NEO": "A320", "A321NEO": "A321", "Q400": "Dash 8-400", "DASH8": "Dash 8"}  # fmt: skip


MAKE_WORDS = re.compile(r"^(BOEING|AIRBUS|BOMBARDIER|CANADAIR|EMBRAER|MCDONNELL DOUGLAS)\s+", re.IGNORECASE)


def normalise_query_model(term: str) -> str:
    """An aircraft as a person, the fleet table or the model names it -> the canonical model or family.
    "Boeing 737-800" -> "737-800", "CL-600-2D24" -> "CRJ900", "737 MAX 8" -> "737-8"; "A320" stays a family."""
    term = MAKE_WORDS.sub("", re.sub(r"\(.*?\)", "", term).strip())
    key = re.sub(r"[\s\-]", "", term.upper())
    if key in QUERY_ALIASES:
        return QUERY_ALIASES[key]
    if re.fullmatch(r"7[0-8]7", key):  # a bare family: "737" is not the 737-7
        return key
    for make in ("BOEING", "AIRBUS", "CNDAIR", "EMB", "DOUG"):
        hit = normalise_model(make, key)
        if hit[1] is not None:
            return hit[0]
    return term
