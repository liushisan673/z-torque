---
name: test-skill
description: 统计输入文本的总字数、汉字数、英文单词数
implementation: md_instruction
version: 1
input_schema:
  type: object
  properties:
    content:
      type: string
      description: 需要统计字数的原始文本内容
  required: ["content"]
output_schema:
  type: object
  properties:
    total_chars:
      type: integer
    chinese_chars:
      type: integer
    english_words:
      type: integer
  required: ["total_chars", "chinese_chars", "english_words"]
allow_scripts: false
required_permissions: []
---

# 技能操作手册：text-word-count
## 任务目标
对传入的content文本做字数统计，输出结构化统计结果。

## 执行步骤
1. 获取入参 content 字符串。
2. 统计指标：
- 总字符数：全部字符（包含标点、空格）
- 汉字数量：只统计中文字符
- 英文单词数：按空白分割，过滤空字符串后计数
3. 严格按照下面固定JSON格式输出，不要额外闲聊解释，只输出JSON。

## 输出格式（强制）
```json
{
  "total_chars": 数字,
  "chinese_chars": 数字,
  "english_words": 数字
}
