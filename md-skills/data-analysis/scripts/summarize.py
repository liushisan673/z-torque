import json
import sys

payload = json.load(sys.stdin)
values = payload.get("values", [])
json.dump({"count": len(values)}, sys.stdout)
