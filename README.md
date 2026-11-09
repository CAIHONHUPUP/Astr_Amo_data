# 我想永远记住你（Astr_Amo_data）

> GitHub：https://github.com/GM888888456/Astrbot_Amo_data
>
> 反馈、Bug、建议请加 QQ 群：**1055679275**

给「我想永远陪伴你」（Astrbot_Amo_forever）配套的**轻量数据后端**。

陪伴插件只负责聊天和心情，**记忆、画像、关系网、日程缓存**都在这里。

## 为什么单独拆一个插件

原版 memory_companion 功能很全，但也很重：五元组哈希身份、P5 鉴权、证据链、审计、
多人格嵌套命名空间……对只服务一个陪伴机器人的场景来说用不上，维护成本还高。

这个插件按同样的功能思路**独立重写**，只保留真正需要的部分：

| 保留 | 砍掉 |
| --- | --- |
| 画像（名字/称呼/关系/备注）可读可写 | 五元组哈希 person_id、身份契约 |
| 记忆按 bot + uid + 群存储、FTS5 检索 | P5 鉴权、attestation、权限闸门 |
| 关系网 | 证据链、溯源审计、纠正回执 |
| 日程/世界观缓存 | 多人格嵌套、复杂 namespace |
| 多 bot 隔离（bot_id） | 海量测试用例、管理后台冗余功能 |
| 同一个人跨平台合并（QQ / 飞书 / Telegram…） | 跨平台账号密码、OAuth、统一登录 |

## 存储位置

```
data/plugin_data/Astr_Amo_data/amo_data.db
```

SQLite + FTS5，只用标准库，不需要装任何依赖。

## 给 Amo 调的接口

插件实例上挂着 `mem`，Amo 拿到实例后直接 await：

```python
await plugin.mem.get_profile(bot_id, uid)
await plugin.mem.save_profile(bot_id, uid, name="老公", relation="相恋")
await plugin.mem.remember(bot_id, uid, "他养了一只叫团子的猫", kind="fact", importance=0.6)
await plugin.mem.recall(bot_id, uid, "猫", limit=6)
await plugin.mem.delete_memory(bot_id, memory_id)

await plugin.mem.list_relations(bot_id)
await plugin.mem.save_relation(bot_id, a_uid, b_uid, "朋友", "大学同学")
await plugin.mem.delete_relation(bot_id, a_uid, b_uid)

await plugin.mem.person_context(bot_id, "lark", "ou_abc", 8)   # 这个人在别的平台说过什么
await plugin.mem.save_profile(bot_id, uid, name="阿墨", platform="aiocqhttp")

await plugin.mem.cache_get(bot_id, "schedule:2026-11-09")
await plugin.mem.cache_set(bot_id, "schedule:2026-11-09", data, ttl=86400)
```

画像的 `name` / `alias` / `relation` / `remark` 都可以随时改写再存回去，
`save_profile` 只更新传进来的字段，没传的保持原值。

## 面板

插件详情 → 查看页面：

- **画像**：搜索、编辑名字/别名/关系/备注、删除（连带记忆和关系边）、查看和新增/删除记忆；
- **人物**：同一个人在不同平台（QQ / 飞书 / Telegram…）会并到一条下面，能看到他在这边说、在那边说了什么；
  名字一样会自动并，并错了点进去「解绑」，也能手动把两条并成一条；
- **关系网**：查看、添加、删除两个人之间的关系。

## 配置

| 配置 | 说明 |
| --- | --- |
| `bot_id` | 数据归属的机器人 ID，留空用 `default` |
| `auto_create_profile` | 有人说话就自动建空画像 |
| `max_recall` | 默认召回条数 |

## 版本

- 2.4.8：仓库改名为 Astrbot_Amo_data（插件 ID 仍是 Astr_Amo_data），链接同步。
- 2.4.6：补齐时间回看 / 每日总结 / 里程碑接口（recall_messages / messages_since）。
- 2.4.4：修好数据面板「聊天」页（之前没有加载函数），接口加超时。
- 2.4.2：画像页改只读，修改统一回陪伴面板。
- 2.4.0：关系图球半径随画布自适应，窄屏不顶边。
- 2.3.8：旧数据导入后也能召回（scope 兼容 / 候选池补 id / 中性新鲜度 / 去重）。
- 2.3.6：关系图改成 3D 神经元球（拖动旋转 / 滚轮缩放 / 双击回正）。
- 2.3.4：关系图可拖动（坐标对齐 + pointer 事件）。
- 2.3.2：关系网里的 `bot` 节点显示成「她」。
- 2.3.0：召回改成 BM25（单字+二元组）+ 重要度 + 遗忘曲线，短词「上班」能命中「上夜班」。
- 2.2.8：压缩摘要改用 summary 角色，不再冒充角色自述。
- 2.2.6：关系网提到第一个页签，只保留只读图谱（增删在陪伴面板）。
- 2.2.4：关系网页加力导向图谱（可拖动、按关系类型着色）。
- 2.2.2：关系网列表显示名字，方便面板核对。
- 2.2.0：`list_messages` 支持按 uid 查，供陪伴的上下文钩子用。
- 2.0.8：修聚合模式下画像详情为空（跨 bot 定位）。
- 2.0.6：面板聊天排版修复 + 手动绑定平台身份。
- 2.0.4：跨平台人物合并（QQ / 飞书同一个人的聊天记录、画像能并到一起）。
- 2.0.2：面板聚合所有 bot，修「没有任何数据」。
- 1.0.0：首版。画像可编辑、记忆读写、FTS5 检索、关系网、缓存。

## 反馈与维护

- 仓库：https://github.com/GM888888456/Astrbot_Amo_data
- QQ 群：**1055679275**（和陪伴插件同一个群）
- 数据库位置：`data/plugin_data/Astr_Amo_data/amo_data.db`

## 保留策略

| 数据 | 默认保留 |
| --- | --- |
| 原始聊天记录 | 14 天（3 天前的会压成按天摘要，到期删除） |
| 运行日志 | 7 天 |
| 日程 / 世界观缓存 | 跟随陪伴插件，面板可手动删 |

面板里「聊天 / 日程 / 日志」三个页签只提供查看和删除，不提供重建——
日程什么时候生成、什么时候遗忘，仍然由陪伴插件决定。
