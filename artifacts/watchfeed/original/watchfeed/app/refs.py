"""Known reference prefixes -> (brand, family). Overrides AI guesses so staff never see a Daytona labelled Submariner.
Checked longest-prefix-first against the normalized reference (uppercase, no spaces/dots/dashes/slashes).
Add your own lines as needed."""
from typing import Optional, Tuple

ROLEX = {
    # Daytona
    "1165": "Daytona", "1265": "Daytona",
    # Submariner
    "124060": "Submariner", "114060": "Submariner", "126610": "Submariner", "126613": "Submariner",
    "126618": "Submariner", "126619": "Submariner", "116610": "Submariner", "116613": "Submariner",
    "116618": "Submariner", "116619": "Submariner",
    # GMT-Master II
    "1267": "GMT-Master II", "1167": "GMT-Master II",
    # Sea-Dweller / Deepsea
    "126600": "Sea-Dweller", "126603": "Sea-Dweller", "126660": "Deepsea", "136660": "Deepsea",
    # Day-Date
    "2282": "Day-Date 40", "2283": "Day-Date 40", "1282": "Day-Date 36", "1182": "Day-Date 36",
    "1183": "Day-Date 36", "1283": "Day-Date 36",
    # Datejust
    "1262": "Datejust 36", "1162": "Datejust 36", "1263": "Datejust 41", "2782": "Datejust 31",
    "2783": "Datejust 31", "2784": "Datejust 31",
    # Others
    "3262": "Sky-Dweller", "3269": "Sky-Dweller", "3369": "Sky-Dweller",
    "126622": "Yacht-Master 40", "126655": "Yacht-Master 40", "226659": "Yacht-Master 42",
    "226627": "Yacht-Master 42", "226658": "Yacht-Master 42", "268622": "Yacht-Master 37",
    "124270": "Explorer", "224270": "Explorer", "124273": "Explorer", "226570": "Explorer II",
    "216570": "Explorer II", "126900": "Air-King", "116900": "Air-King", "116400": "Milgauss",
}

PATEK = {
    "5711": "Nautilus", "5712": "Nautilus", "5726": "Nautilus", "5980": "Nautilus", "5990": "Nautilus",
    "5811": "Nautilus", "7118": "Nautilus", "7010": "Nautilus", "3800": "Nautilus", "5740": "Nautilus",
    "5167": "Aquanaut", "5168": "Aquanaut", "5164": "Aquanaut", "5968": "Aquanaut", "5269": "Aquanaut",
    "5261": "Aquanaut", "5267": "Aquanaut", "5065": "Aquanaut",
    "5270": "Grand Complications", "5204": "Grand Complications", "5320": "Grand Complications",
    "5327": "Grand Complications", "5520": "Grand Complications", "5905": "Complications",
    "5930": "Complications", "5961": "Complications", "5960": "Complications",
    "5396": "Complications", "4947": "Complications", "5227": "Calatrava", "6119": "Calatrava", "5196": "Calatrava",
    "7300": "Twenty~4", "4910": "Twenty~4",
}

AP = {
    "15500": "Royal Oak", "15510": "Royal Oak", "15202": "Royal Oak Jumbo", "16202": "Royal Oak Jumbo",
    "15400": "Royal Oak", "15450": "Royal Oak", "15300": "Royal Oak", "26240": "Royal Oak Chronograph",
    "26331": "Royal Oak Chronograph", "26320": "Royal Oak Chronograph", "26715": "Royal Oak Chronograph",
    "26574": "Royal Oak Perpetual Calendar", "15407": "Royal Oak Double Balance",
    "77350": "Royal Oak 34", "77351": "Royal Oak 34", "67650": "Royal Oak 33", "15550": "Royal Oak 37",
    "15551": "Royal Oak 37", "26400": "Royal Oak Offshore", "26405": "Royal Oak Offshore",
    "26420": "Royal Oak Offshore", "26470": "Royal Oak Offshore", "26238": "Royal Oak Offshore",
    "26231": "Royal Oak Offshore", "15710": "Royal Oak Offshore Diver", "15720": "Royal Oak Offshore Diver",
    "26393": "Code 11.59",
}

TABLES = [("Rolex", ROLEX), ("Patek Philippe", PATEK), ("Audemars Piguet", AP)]


def lookup(ref_norm: Optional[str], brand: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (brand, family) for a normalized reference, or (None, None) if unknown."""
    if not ref_norm:
        return None, None
    b = (brand or "").lower()
    for tbrand, table in TABLES:
        if b and b not in tbrand.lower():
            continue  # AI already identified another brand (e.g. Tudor, Omega)
        for n in range(len(ref_norm), 3, -1):
            fam = table.get(ref_norm[:n])
            if fam:
                return tbrand, fam
    return None, None
