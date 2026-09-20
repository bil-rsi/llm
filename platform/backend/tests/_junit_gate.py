"""Quality gate on a JUnit report: at least one test, zero failures, errors and skips (CONSTRAINTS.md)."""

import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root[0]
tests, failures, errors, skipped = (int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped"))
print(f"gate: tests={tests} failures={failures} errors={errors} skipped={skipped}")
sys.exit(0 if tests >= 1 and failures == 0 and errors == 0 and skipped == 0 else 1)
