# MiniMax H3 Video Studio 发布规范

本文约束 MiniMax H3 Video Studio 的版本、Git 分支、Tag、GitHub Release 与 Changelog。准备发布不等于允许发布：只有用户在当前任务中明确要求“发布到 GitHub”时，才可以执行任何远端写操作。

## 版本合同

- 使用语义化版本 `MAJOR.MINOR.PATCH`。
- `package.json` 是唯一版本源；`package-lock.json` 必须保持一致。
- GitHub 默认分支是 `main`，不另建或同步 `master` 分支。
- Tag 使用注释 Tag，格式为 `vX.Y.Z`。
- GitHub Release 标题与 Tag 一致，均为 `vX.Y.Z`。
- `CHANGELOG.md` 使用 `[X.Y.Z] - YYYY-MM-DD`，未发布变化只放在 `[Unreleased]`。

版本选择：

- `PATCH`：向后兼容的修复、文档或运维修订。
- `MINOR`：向后兼容的新功能。
- `MAJOR`：破坏性 API、数据合同或迁移要求。

## 发布前检查

1. 确认用户已在当前任务中明确授权发布到 GitHub，并确认目标版本号。
2. 检查工作树与分支，保护用户和其他 Agent 的未提交改动。
3. 更新 `package.json` 与 `package-lock.json` 的同一版本号。
4. 将 `CHANGELOG.md` 的已发布内容从 `[Unreleased]` 移入对应版本段，保留空的 `[Unreleased]`。
5. 按风险完成相关测试和完整 `npm test`，修复后重新运行失败项与完整检查。
6. 扫描密钥、Token、账号、机器路径、模型权重、用户素材、生成结果和运行数据。
7. 确认发布提交位于 `main`，且文档、代码、测试结果和实际部署状态一致。

## 获得授权后的发布顺序

```bash
# 示例只展示顺序；X.Y.Z 必须替换为已确认版本。
git tag -a vX.Y.Z -m "MiniMax H3 Video Studio vX.Y.Z"
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --notes-from-tag
```

发布后必须复核：

- GitHub 默认分支仍为 `main`。
- `main`、`vX.Y.Z` 与 GitHub Release 指向同一发布提交。
- GitHub 页面中的版本号、Changelog 和 Release Notes 一致。
- Secret Scanning 与 Push Protection 保持启用。
- 部署健康检查通过；失败时回滚部署，不移动或重写已公开 Tag。

## 禁止事项

没有用户当次明确授权时，禁止：

- `git push` 或任何强制推送；
- 创建、移动、删除或推送 Tag；
- 创建、编辑、发布或删除 GitHub Release；
- 创建或删除 GitHub 仓库、改变可见性或默认分支；
- 把本地私有历史、验收证据、模型、素材、结果或密钥带入公开历史。
