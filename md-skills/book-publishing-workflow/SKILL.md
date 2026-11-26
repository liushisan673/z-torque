---
name: book-publishing-workflow
description: Coordinate educational publishing work from research through manuscript, proofing, quality control, and launch.
implementation: md_instruction
version: 1
input_schema:
  type: object
  properties:
    subject: {type: string}
    grade_band: {type: string}
    region: {type: string}
    curriculum_version: {type: string}
    textbook_version: {type: string}
    target_pages: {type: integer, minimum: 1}
    deliverable: {type: string}
  required: [subject, grade_band, region, curriculum_version, textbook_version, target_pages, deliverable]
  additionalProperties: false
output_schema:
  type: object
  properties:
    work_order: {type: object}
    stages: {type: array, items: {type: object}}
  required: [work_order, stages]
allow_scripts: false
required_permissions: []
---

# 教辅出版项目总流程

开工前必须确认学科、学段、地区、课标版本、教材版本、目标页码、产品形态和交付物。不得把初中物理、人教版或某一地区作为默认事实。

流程阶段与交付物：
1. 课标研究：课标原文、学业质量要求、内容要求、学段边界、课标—教材映射表。
2. 教材研读：章节知识结构、实验/公式/易错点清单。
3. 考情研究：真题与模拟题分开统计，按年份、地区、题型、知识点记录证据；不得用检索片段数冒充频次。
4. 竞品与调研：竞品证据表；实地/电话/网络调研记录样本、时间、方法、原始证据和局限。
5. 体例设计：目录、栏目、题量、难度、页码预算、双向细目表，并校验总页数。
6. 组稿命题：记录题目来源/原创标记、知识点、答案解析、难度和适用范围；查重必须调用确定性工具。
7. 录排制作：以确认稿件为输入，交付初录 PDF、源文件、图片重绘清单、字体/公式/图表检查表；未提供初录稿不得声称排版完成。
8. 三校三审：校对员与责任编辑必须分离。校对检查文字、数字、符号、格式；责任编辑审核知识准确性、课标适配、结构和出版规范。打回/通过必须人工确认。
9. 通读质检：核对术语、编号、答案引用；PDF 页面、色值、印张、书脊、拼版等逐项标注自动核验/人工核验/缺少依据。
10. 卖点培训与售后：卖点回链真实内容证据；培训基于确认版目录和样章；售后仅使用已绑定政策/产品资料。

引用统一为 [资料:文件名（页码/段落位置）]，禁止 chunk UUID、内部路径和虚构引用。硬统计、页码预算、差错率、查重和文本差异必须调用确定性检查工具。缺少课标、模拟题、竞品或初录文件时明确标注输入缺口。

差错清单字段：类型、位置、原文、建议、严重度、轮次、证据来源、处理状态。跨 Agent 交接字段：项目参数、输入版本、已完成工作、结论及证据、未解决问题、输出文件/资料、下一步动作。
