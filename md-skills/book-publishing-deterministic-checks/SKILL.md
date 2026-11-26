---
name: book-publishing-deterministic-checks
description: Deterministically calculate exam frequency, page budgets, error rates, duplicates, and manuscript text differences.
implementation: md_script
version: 1
windows_trusted: true
input_schema:
  type: object
  properties:
    action: {type: string, enum: [frequency, page_budget, error_rate, duplicate_check, text_diff]}
    records: {type: array, items: {type: object}}
    total_pages: {type: number, minimum: 0}
    allocated_pages: {type: number, minimum: 0}
    error_count: {type: integer, minimum: 0}
    character_count: {type: integer, minimum: 1}
    threshold_per_10000: {type: number, minimum: 0}
    texts: {type: array, items: {type: string}}
    original_text: {type: string}
    revised_text: {type: string}
  required: [action]
  additionalProperties: false
output_schema:
  type: object
  properties:
    action: {type: string}
    result: {type: object}
  required: [action, result]
allow_scripts: true
required_permissions: []
---

# 出版确定性检查

用于真题知识点频次、页码预算、差错率、文本重复和修订差异。只对传入数据计算，不自行补齐缺失数据。

- frequency：records 每条至少包含 topic，可选 year/region/question_id；按主题、年份、地区计数并返回样本数。
- page_budget：比较 total_pages 与 allocated_pages，返回差值和是否自洽。
- error_rate：按 error_count / character_count × 10000 计算万字差错率，并与阈值比较。
- duplicate_check：对 texts 做规范化精确重复检查，输出重复项索引；不把语义相似冒充精确查重。
- text_diff：输出 original_text 与 revised_text 的统一文本差异。

本工具不能验证 PDF 色值、拼版、折手、书脊或物理印张；这些项目必须标记“需印前软件/人工核验”。脚本执行受 Task 人工审批、沙箱和 Windows 可信声明约束。
