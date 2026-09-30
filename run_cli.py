import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from resource_review.contracts import ResourceVersion, ReviewFinding

entity = ResourceVersion("E-DEMO", "智能教学资源风险审校", 1)
record = ReviewFinding("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
