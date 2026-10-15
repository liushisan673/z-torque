---
name: data-analysis
description: Perform deterministic analysis through an explicitly approved script.
implementation: md_script
version: 1
input_schema:
  type: object
  properties:
    values:
      type: array
      items:
        type: number
  required:
    - values
output_schema:
  type: object
  properties:
    count:
      type: integer
  required:
    - count
allow_scripts: true
required_permissions: []
---

# Data Analysis

Use the approved script only when script execution is enabled by the runtime.
