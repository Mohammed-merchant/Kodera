import json
from pathlib import Path

FILE = Path("train_data/assistant_data.jsonl")

valid = 0
invalid = 0
errors = []

required_roles = {"system", "user", "assistant"}

with FILE.open("r", encoding="utf-8") as f:
    for line_no, line in enumerate(f, start=1):
        try:
            item = json.loads(line)
            messages = item["messages"]

            if not isinstance(messages, list) or not messages:
                raise ValueError(f"Line {line_no}: Messages must be a non-empty list")

            roles = [m["role"] for m in messages]

            if roles[0] != "system":
                raise ValueError(f"Line {line_no}: Missing system message")

            if "user" not in roles or "assistant" not in roles:
                raise ValueError(f"Line {line_no}: Missing user or assistant message")

            for msg in messages:
                if msg["role"] not in required_roles:
                    raise ValueError(f"Line {line_no}: Invalid role '{msg['role']}' found. Allowed roles are {required_roles}.")

                if not isinstance(msg["content"], str) or not msg["content"].strip():
                    raise ValueError(f"Line {line_no}: Empty or invalid content")

            valid += 1

        except Exception as e:
            invalid += 1
            errors.append(f"Line {line_no}: {e}")

print("\n===== KODEARA DATASET REPORT =====")
print("Valid examples:", valid)
print("Invalid examples:", invalid)
print("Total examples:", valid + invalid)

for error in errors:
    print(error)

if invalid == 0:
    print("\nDataset structure looks good!")