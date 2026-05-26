def parse_direction(val):
    """
    Normalizes direction inputs from multiple sources.
    Accepts: BUY/SELL, long/short, green/red
    Returns: "BUY", "SELL", or None
    """
    if val is None:
        return None
    d = str(val).strip().lower()
    if d in ("buy", "long", "green", "g"):
        return "BUY"
    if d in ("sell", "short", "red", "r"):
        return "SELL"
    return None


def normalize_bar_id(bar_id):
    if bar_id is None:
        return None
    try:
        if bar_id != "":
            return int(float(bar_id))
        return None
    except Exception:
        try:
            return str(bar_id)
        except Exception:
            return None
