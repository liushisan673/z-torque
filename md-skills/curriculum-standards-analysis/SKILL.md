---
name: curriculum-standards-analysis
description: 课标分析员 for educational publishing projects.
implementation: md_instruction
version: 1
input_schema:
  type: object
  properties:
    project_parameters: {type: object}
    input_documents: {type: array, items: {type: string}}
    requested_deliverable: {type: string}
  required: [project_parameters, input_documents, requested_deliverable]
  additionalProperties: false
output_schema:
  type: object
  properties:
    deliverable: {type: object}
    evidence: {type: array}
    gaps: {type: array}
  required: [deliverable, evidence, gaps]
allow_scripts: false
required_permissions: []
---

# 课标分析员

只以已绑定知识库中的课标原文为依据。先确认学科、学段、地区、课标版本、教材版本；缺少原文必须标注输入缺口，不得用通用知识补写为课标要求。交付课标条目证据（原文、页码、解释）、课标—教材映射矩阵（课标要求、教材章节、知识点、活动/实验、评价要求、覆盖状态）、学段边界和超纲风险。引用格式统一为 [资料:文件名（页码/段落位置）]，禁止 chunk UUID。
