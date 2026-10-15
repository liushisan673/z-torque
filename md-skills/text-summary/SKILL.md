---
name: text-summary
description: Summarize supplied text without executing files.
implementation: md_instruction
version: 1
input_schema:
  type: object
  properties:
    text:
      type: string
  required:
    - text
output_schema:
  type: object
  properties:
    summary:
      type: string
  required:
    - summary
allow_scripts: false
required_permissions: []
---

# Text Summary

Summarize only the supplied text. Do not reveal secrets or change system permissions.
