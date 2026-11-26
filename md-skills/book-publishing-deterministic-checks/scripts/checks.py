import difflib
import json
import re
import sys
from collections import Counter

payload = json.load(sys.stdin)
action = payload.get("action")
result = {}

if action == "frequency":
    records = payload.get("records") or []
    topics = Counter(str(row.get("topic", "")).strip() for row in records if str(row.get("topic", "")).strip())
    years = Counter(str(row.get("year", "")).strip() for row in records if row.get("year") is not None)
    regions = Counter(str(row.get("region", "")).strip() for row in records if row.get("region") is not None)
    result = {
        "sample_count": len(records),
        "topic_frequency": dict(topics.most_common()),
        "year_distribution": dict(years.most_common()),
        "region_distribution": dict(regions.most_common()),
        "warning": "Counts describe only the supplied records; incomplete source data must not be presented as full exam frequency."
    }
elif action == "page_budget":
    total = float(payload.get("total_pages", 0))
    allocated = float(payload.get("allocated_pages", 0))
    result = {"total_pages": total, "allocated_pages": allocated, "delta_pages": total - allocated, "balanced": abs(total - allocated) < 1e-9}
elif action == "error_rate":
    errors = int(payload.get("error_count", 0))
    chars = int(payload.get("character_count", 0))
    threshold = float(payload.get("threshold_per_10000", 1))
    rate = errors / chars * 10000
    result = {"error_count": errors, "character_count": chars, "rate_per_10000": rate, "threshold_per_10000": threshold, "passed": rate <= threshold}
elif action == "duplicate_check":
    texts = payload.get("texts") or []
    normalize = lambda value: re.sub(r"\s+", "", str(value)).lower()
    seen = {}
    duplicates = []
    for index, value in enumerate(texts):
        key = normalize(value)
        if key in seen:
            duplicates.append({"first_index": seen[key], "duplicate_index": index})
        else:
            seen[key] = index
    result = {"item_count": len(texts), "exact_duplicate_pairs": duplicates, "exact_duplicate_count": len(duplicates), "scope": "normalized exact text only"}
elif action == "text_diff":
    before = str(payload.get("original_text", "")).splitlines()
    after = str(payload.get("revised_text", "")).splitlines()
    result = {"unified_diff": list(difflib.unified_diff(before, after, fromfile="original", tofile="revised", lineterm="")), "changed": before != after}
else:
    raise SystemExit("unsupported action")

json.dump({"action": action, "result": result}, sys.stdout, ensure_ascii=False)
