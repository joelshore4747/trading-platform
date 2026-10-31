#!/usr/bin/env bash
# PreToolUse hook (Write|Edit): refuse to write anything that looks like a credential.
#
# This repository has no live trading path and must not acquire one by accident.
# Exchange API keys are the single asset here worth stealing, so the cheapest
# insurance is never letting one reach the working tree in the first place.
#
# Exit 2 = block the tool call and show stderr to Claude.

set -uo pipefail

payload=$(cat)

python3 - "$payload" <<'PY'
import json
import re
import sys

try:
    data = json.loads(sys.argv[1])
except (json.JSONDecodeError, IndexError):
    sys.exit(0)  # Malformed payload: fail open rather than blocking all writes.

tool_input = data.get("tool_input") or {}
path = tool_input.get("file_path", "")

# Gather whatever text this call would write.
chunks = [
    tool_input.get("content", ""),
    tool_input.get("new_string", ""),
]
for edit in tool_input.get("edits") or []:
    chunks.append(edit.get("new_string", ""))
text = "\n".join(c for c in chunks if isinstance(c, str))

if not text:
    sys.exit(0)

# Example files are the sanctioned place for placeholder-shaped strings.
if path.endswith((".example", ".sample", ".template")) or "/.env.example" in path:
    sys.exit(0)

PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key ID"),
    (r"sk-[A-Za-z0-9]{32,}", "OpenAI-style secret key"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic API key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "private key block"),
    # Exchange credentials assigned a long literal, e.g. api_secret = "abc...".
    (
        r"(?i)\b(api[_-]?secret|api[_-]?key|secret[_-]?key|private[_-]?key)\b\s*[=:]\s*['\"][A-Za-z0-9/+_\-]{24,}['\"]",
        "hardcoded exchange credential",
    ),
]

hits = []
for pattern, label in PATTERNS:
    if re.search(pattern, text):
        hits.append(label)

if hits:
    print(
        "BLOCKED: this write appears to contain a credential "
        f"({', '.join(sorted(set(hits)))}) targeting {path or 'an unnamed file'}.\n\n"
        "This repository must never hold live credentials. If you need a secret at "
        "runtime, read it from an environment variable and document the variable "
        "name in .env.example instead.\n\n"
        "If this is a false positive (a test fixture or documentation example), "
        "shorten the literal or move it to a file ending in .example.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
PY
