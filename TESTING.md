# TESTING — 离线三方合并验证记录

运行环境：Python 3.11 本地 venv（FastAPI 0.115.6 / SQLAlchemy 2.0.36 / PyJWT / redis-py / pytest），
数据库为 SQLite 内存库（StaticPool 单连接）；MySQL 下 `SELECT ... FOR UPDATE` 走相同 SQL，
SQLite 不支持该子句时服务层回退到 `db.get`，由单写者串行保证等价。无需 Redis/MySQL 容器。

运行方式（在 `backend/` 目录）：

```bash
python -m pytest tests -p no:cacheprovider -q
python tests/test_merge_engine.py        # 纯函数引擎，独立可运行
```

## 结果（当前实测）

```
30 passed in ~1.7s
```

|文件|用例数|覆盖内容|
|---|---|---|
|`tests/test_merge_engine.py`|16|纯函数三方合并，无数据库|
|`tests/test_merge_sessions.py`|11|SQLite 服务层：幂等、漂移重算、原子替换、回滚、旧快照|
|`tests/test_merge_api.py`|3|FastAPI TestClient 端到端：路由、鉴权、409/403 合约|

## 需求点 → 验证映射

- 稳定身份跟踪改名：`test_rename_followed_by_one_sided_edit_tracks_identity`、`test_rename_plus_other_side_value_edit_combines`
- 双方改不同测值自动合并（字段级）：`test_distinct_field_edits_merge_automatically`
- 同项异改留待裁决：`test_same_item_different_edits_need_adjudication`、`test_overlapping_field_edits_still_conflict`
- 撤回遇修改：`test_delete_vs_modify_conflict_and_keep_or_delete`、`test_both_delete_is_clean...`
- 编号占用：`test_one_sided_rename_into_occupied_key_is_conflict`、`test_single_side_duplicate_keys...`
- 编号互换整体落地：`test_key_swap_lands_without_conflict`
- 双方新增撞号：`test_add_collision_both_sides_same_new_key`
- 先前裁决仅在三方内容未变时沿用：`test_prior_decisions_reused_only_while_fingerprint_matches`
- 并发/重试不丢改动、不重复新增：`test_package_retry_is_idempotent_and_never_duplicates`、`test_two_labs_concurrent_intake_joins_one_session_without_loss`、`test_new_rows_with_client_refs_are_added_once`
- 冲突未决不改正式观测、未裁决落地被 409 拒绝：`test_conflict_holds_formal_data_until_adjudicated_and_applied`
- 裁决期间正式批次变化重算：`test_drift_during_adjudication_recomputes_and_keeps_valid_decisions`（无关行漂移保留正式新值、有效裁决沿用）、`test_drift_that_changes_conflict_content_drops_prior_decision`（冲突内容变化则旧裁决失效）
- 整批原子替换与中断回滚：`test_swap_lands_and_rollback_keeps_formal_batch`（注入写失败，正式批次零变化后重试成功）
- 旧研究保持原样：`test_old_runs_keep_their_frozen_snapshots_after_merge`
- HTTP 权限（viewer 403、admin 专属落地、幂等回放）：`tests/test_merge_api.py`

## 前后端校验和一致性

前端 `sha256(canonical(...))` 与后端 `src/utils/serializers.py:checksum` 均对字典键深度排序，
已用 Node 与 Python 对同一含中文、含嵌套字典的载荷交叉验证，摘要完全一致：

```
JS 0c0281b90352b74c3a7b45d5fa97f59b7c8ef39206b81a2f1a866b44525e08eb
PY 0c0281b90352b74c3a7b45d5fa97f59b7c8ef39206b81a2f1a866b44525e08eb
```

## 未覆盖/边界

- 未做真实 MySQL/InnoDB 锁与多线程压测（本地无容器）；行锁语义由代码路径与 SQLite 串行测试间接保证。
- 前端为原生 ES modules，仅做 `node --check` 语法校验，未引入浏览器端自动化测试。
