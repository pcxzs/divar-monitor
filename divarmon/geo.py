"""Geo fencing: Divar honours the map polygon loosely, so alerts are
re-checked client side against the polygon encoded in the search link."""

from . import log

logger = log.get(__name__)


def decode_polyline(s: str, precision: int = 5) -> list:
    """Google-style encoded polyline -> [(lat, lng), ...]."""
    coords, lat, lng, i, factor = [], 0, 0, 0, 10.0 ** precision
    while i < len(s):
        for which in (0, 1):
            result, shift = 0, 0
            while True:
                if i >= len(s):
                    raise ValueError("Truncated polyline")
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if (result & 1) else (result >> 1)
            if which == 0:
                lat += d
            else:
                lng += d
        coords.append((lat / factor, lng / factor))
    return coords


def point_in_polygon(lat: float, lng: float, poly: list) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i][1], poly[i][0]
        x2, y2 = poly[(i + 1) % n][1], poly[(i + 1) % n][0]
        if ((y1 > lat) != (y2 > lat)) and (lng < (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def build_fence(spec: dict) -> dict | None:
    """Decoded polygons + bbox from a parsed search spec. None = no fencing."""
    polys = []
    try:
        vals = (spec.get("form_data", {}).get("multi_geo_polygon_encoded", {})
                .get("repeated_string", {}).get("value", []))
        for v in vals:
            pts = decode_polyline(v.strip().rstrip(","))
            if len(pts) >= 3 and all(-90 <= la <= 90 and -180 <= ln <= 180 for la, ln in pts):
                polys.append(pts)
    except Exception as e:
        logger.warning(f"[{spec.get('name', '?')}] Could not decode search polygon: {e}")
    bbox = None
    try:
        nums = [f["value"] for f in spec.get("form_data", {}).get("bbox", {})
                .get("repeated_float", {}).get("value", [])]
        if len(nums) == 4:
            lon1, lat1, lon2, lat2 = nums
            bbox = (min(lat1, lat2), min(lon1, lon2), max(lat1, lat2), max(lon1, lon2))
    except Exception:
        pass
    if not polys and bbox is None:
        return None
    return {"polygons": polys, "bbox": bbox}


def allowed(fence: dict | None, lat, lng) -> bool:
    """True if the point may be alerted. Fail-open when unverifiable."""
    if not fence or lat is None or lng is None:
        return True
    try:
        if fence.get("polygons"):
            return any(point_in_polygon(float(lat), float(lng), p) for p in fence["polygons"])
        bb = fence.get("bbox")
        if bb:
            min_lat, min_lng, max_lat, max_lng = bb
            return min_lat <= float(lat) <= max_lat and min_lng <= float(lng) <= max_lng
    except Exception:
        return True
    return True
