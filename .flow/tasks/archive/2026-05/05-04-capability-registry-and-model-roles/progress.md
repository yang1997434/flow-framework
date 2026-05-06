# progress.md — capability-registry-and-model-roles

## Plan

(single, main session implements) — 子项目 #2 + #2b 合批：capability registry + 模型 role 抽象。
来源 audit task `05-04-audit-flow-issues/prd.md` 子项目 #2/#2b。

## Execute Log

| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-04 | main | `claude/capabilities/defaults.json` | 13 capability + 5 model role 默认映射 |
| 2026-05-04 | main | `scripts/flow_capability.py` | resolver + render() with dotted access (`.args.mode`, `.follow_with`) + CLI |
| 2026-05-04 | main | 编辑 10 个 prompt 文件 | 26 capability + 7 model role 占位完成 |
| 2026-05-04 | main | `flow_install.py` 加 `render-prompts` + 安全护栏 | 拒绝写穿透 symlink-into-source（防 P0 误覆盖） |
| 2026-05-04 | main | **P0 incident**: 旧 symlink 让 render 写穿透到 source | git checkout 还原 + 删 symlink + 重做 21 处 edit + 加护栏 |
| 2026-05-04 | main | `install.sh` 改 symlink → render 流程 | 检测旧 symlink 自动删；render 写真实文件 |
| 2026-05-04 | main | `tests/smoke/test_capability.py` | 14 cases（registry/render/dotted/anti-regression）|
| 2026-05-04 | main | `flow_selftest.py` 加 rendered 检查 | 第 5 类：~/.claude/ 下 prompt 无 `{{}}` 残留 |

## Verify Report

- ✅ Smoke 全集 37/37 pass（10 + 13 + 14）
- ✅ Selftest 6 类全过（hooks/init/task/plugins/rendered/doctor）
- ✅ Anti-regression 已写测试：仓库源中无裸 plugin 引用、无模型名硬编码
- ✅ Rendered 输出 13 个文件全部干净（0 处 `{{}}`）
- ✅ Render 安全护栏：`flow_install.py` 拒绝写入 dst-symlink-into-source 路径；install.sh 装时自动删旧 symlink
- ✅ Issue #415 兼容性保留：模板各 hook 独立 matcher entry 不变
- ✅ Credential grep self-check pass
- ⚠️ `gstack:` plugin 未在 `dependencies.json` 列出（用户私有），相关 capability (cross_model_consult/review/challenge, ui_visual_review, deploy_chain) 在没装 gstack 时 render 仍写出 `gstack:codex` 等字符串 —— Claude 看到会尝试调用并失败；用户应在 `.flow/config.local.yaml` 覆盖映射或装 gstack
- ⚠️ docs/Skills-Phase映射.md 等文档里仍有 100+ 处具体 plugin 名（C 报告归到"低优先级"），未在本批改

## Sediment Notes

**Pattern**: "声明式 capability registry + install-time render"。本批落地的 3 段结构：
1. JSON manifest（默认）
2. Python resolver + 模板替换（带 dotted access）
3. install.sh 渲染写盘
未来可复用到任何"集中替换 prompt 中外部依赖名"的场景。

**Pitfall (P0 incident)**: **render 写穿透 symlink 灾难** —— 当 dst 是 symlink 指向 source 时，写入会**反向覆盖 source 模板**，把所有占位符替换成具体值，等于丢失抽象层。教训：
- **任何"写入 dst"的脚本都必须先检测 dst 是不是 symlink**
- 检测到 symlink-into-source 必须 fail-loud 拒绝
- install.sh 必须在 render 前主动 `rm` 旧 symlink
- 可恢复但必须还有 git history（这次靠 git checkout 救回）
建议 v0.4 完成后 promote 到 vault `pitfalls/`，文件名：`render-write-through-symlink.md`。

**ADR**: 无新 ADR（落在 audit 主 ADR 框架内）。

## Retro

- ✅ Worked: capability + model 在同批做（搭车）；测试覆盖率（14 cases）足够 catch 反退化
- ❌ Didn't: 第一次 render 没检查 dst symlink 状态 → 写穿透事故，源模板全部被覆盖；幸亏 git history 还在
- 💡 框架反馈：所有"写文件"的工具都应该有"如果 dst 是 symlink 我应该怎么做"的策略；现有 install.sh 之前的 symlink 模式与本批的 render 模式天然冲突，过渡期必须显式处理
