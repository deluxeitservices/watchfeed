import re
from typing import Optional


def norm_ref(ref: Optional[str]) -> Optional[str]:
    """'126710 BLNR' / '126710-blnr' -> '126710BLNR'"""
    if not ref:
        return None
    r = re.sub(r"[^A-Z0-9]", "", str(ref).upper())
    return r or None
