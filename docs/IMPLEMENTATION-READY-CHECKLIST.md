# 编码前收口清单（Implementation-Ready Checklist）

> **状态：已冻结（v1）。** 所有 P0–P3 项均已定稿，详细契约见 `docs/IMPLEMENTATION-READY-v1.md`。
> 配套：`docs/MVP PRD.md` · `docs/ADR-001-architecture.md` · `docs/glossary.md`

## 完成状态

| 档位 | 项 | 状态 |
|------|----|------|
| P0 | C1 后端 REST API 契约 | ✅ |
| P0 | C2 连接配置规范 | ✅ |
| P0 | C3 KB 后端选型 | ✅（MaaS Vector KB） |
| P0 | C4 ChangeProfile 字段 | ✅ |
| P0 | C5 Diff Parser 语言规则 | ✅ |
| P1 | M1 LLM Prompt + 结构化输出 | ✅ |
| P1 | M2 KBQuery 生成 | ✅ |
| P1 | M3 Evidence 后校验 | ✅ |
| P1 | M4 Markdown 渲染 | ✅ |
| P1 | M5 失败降级 | ✅ |
| P2 | S1 项目结构 | ✅ |
| P2 | S2 运行/部署 | ✅ |
| P2 | S3 日志脱敏 | ✅ |
| P3 | V1 测试集 Fixtures | ✅ |
| P3 | V2 Eval Harness | ✅ |

## Definition of Ready（已满足）

- [x] GitLab 获取 MR 的输入输出契约已冻结
- [x] ChangeProfile 已冻结
- [x] Diff Parser 语言边界已冻结
- [x] 大 PR 三档阈值已冻结
- [x] KBQuery Schema 已冻结
- [x] CheckReport Schema 已冻结
- [x] Evidence 规则已冻结
- [x] LLM Structured Output 策略已冻结
- [x] Git/KB/LLM 失败行为已冻结
- [x] Secret 存储与日志脱敏已冻结
- [x] 基础项目结构已冻结

→ **可进入正式编码。** 详细契约见 `docs/IMPLEMENTATION-READY-v1.md`。
