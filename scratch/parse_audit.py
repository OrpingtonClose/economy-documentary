import re
from pathlib import Path

log_path = Path("/Users/orpington/.gemini/antigravity/brain/33beca10-33a6-47ac-915f-990b840d1541/.system_generated/tasks/task-546.log")
artifact_path = Path("/Users/orpington/.gemini/antigravity/brain/33beca10-33a6-47ac-915f-990b840d1541/architecture_audit_report.md")

if not log_path.exists():
    print(f"Error: Log file not found at {log_path}")
    exit(1)

log_content = log_path.read_text(encoding="utf-8")

# Let's split the log by the separator "--------------------------------------------------"
blocks = log_content.split("--------------------------------------------------")

report_md = []
report_md.append("# 🕵️ Agentic Architecture Audit - Detailed Findings Report\n")
report_md.append("This report lists the detailed findings from the **architecture tests** audit under the updated black hat threat model. The audit was conducted to verify if any test violates system invariants or attempts to make the **covering tests** ineffectual.\n")
report_md.append("## Summary of Scanned Files\n")

# Let's parse each block
parsed_blocks = []
for block in blocks:
    block = block.strip()
    if not block:
        continue
    
    # Try to find file name
    file_match = re.search(r"File:\s*([a-zA-Z0-9_\-\.]+)", block)
    if file_match:
        filename = file_match.group(1)
        # Check if PASS or FAIL
        is_pass = "PASS" in block and "FAIL" not in block and "VIOLATION" not in block
        status = "✅ PASS" if is_pass else "❌ FAIL"
        
        # Extract body (everything after the File: line)
        body = block
        # Clean up warnings from shell
        body = re.sub(r"cat:.*No such file or directory\n?", "", body)
        
        parsed_blocks.append((filename, status, body))

# Create table of files
report_md.append("| Test File | Audit Status |")
report_md.append("| :--- | :--- |")
for filename, status, _ in parsed_blocks:
    report_md.append(f"| {filename} | {status} |")
report_md.append("\n---\n")

# Write detailed sections
report_md.append("## Detailed Violations and Analysis\n")
for filename, status, body in parsed_blocks:
    if "PASS" in status:
        report_md.append(f"### ✅ {filename}\n")
        report_md.append("This file passed the architectural audit with zero violations detected.\n")
    else:
        report_md.append(f"### ❌ {filename}\n")
        report_md.append("```markdown")
        report_md.append(body)
        report_md.append("```\n")
    report_md.append("---\n")

artifact_path.write_text("\n".join(report_md), encoding="utf-8")
print(f"Successfully compiled report to {artifact_path}")
