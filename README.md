# OCI 镜像末两层合并服务

运行节点已经缓存了基础层，发版时只需下发补丁。本服务把单平台镜像的**末两层补丁合并成一层**，写入本地 OCI 镜像布局（OCI image layout），并以新标签发布，从而减少下发层数。

## 功能与约束

- 输入：本地 OCI 布局中的**原标签**与**新标签**（`POST /merge`）。
- 只处理**单平台**镜像（`index.json` 中标签直接指向 image manifest；manifest list/index 会被拒绝）。
- 末两层必须是**未压缩 tar**，且只含普通文件、目录、普通 whiteout（`.wh.<name>`）、opaque whiteout（`.wh..wh..opq`）；其他条目类型（符号链接、硬链接、设备文件等）会报错拒绝，不会静默产生错误结果。
- 合并语义（与 overlay 应用语义一致）：
  - 删除（whiteout）与目录遮蔽（opaque whiteout）仍然作用于**更低层**——合并层里保留对应的 whiteout / opaque 标记；
  - 同一层在遮蔽目录后新增的文件照常保留；
  - 上层条目覆盖下层同路径条目；下层被遮蔽/删除的条目不会泄漏进合并层；
  - **目录重建不泄漏旧子项**：低层用普通 whiteout 删除目录、或把目录换成普通文件后，高层在原路径显式重建目录时，合并层会保留/合成对应 whiteout，基础层旧子项继续隐藏；同层"whiteout + 显式重建"即使 whiteout 条目在 tar 流中排在新条目之后，新目录和新文件也保留；普通目录叠加仍是并集，低层未被改动的子项继续可见；
  - 合并层输出时 whiteout 始终排在其作用的目标子树之前，保证严格按 tar 顺序解包的应用器也能得到正确结果。
- **基础层只取已有元数据**：新清单直接沿用基础层的 descriptor（摘要不变），从不读取基础层内容。
- **资源占用**：文件正文全程流式处理（两遍扫描：第一遍只读 tar 头建元数据索引，第二遍从源 blob 顺序拷贝数据写入新层），内存占用不随文件正文大小增长；磁盘上除新层 blob 外只暂存元数据（内存中的条目索引），不产生其他中间文件。
- **清单与配置一致**：新 manifest 的 layers = 基础层 descriptors + 合并层 descriptor；新 config 的 `rootfs.diff_ids` 与 history 同步收缩，`config` 段（Cmd/Entrypoint/Env/WorkingDir/User 等启动配置）原样保留。未压缩层的 diff_id 等于 blob 摘要。
- **失败安全**：所有 blob 先写临时文件、fsync 后原子改名；`index.json` 永远最后更新。任何一步失败，原标签保持可用，新标签不会发布；临时文件会被清理。

## 目录结构

```
app/
  main.py       FastAPI 入口（POST /merge、GET /tags、GET /healthz）
  service.py    合并编排：解析标签、重写 config/manifest、原子发布
  merge.py      两层 tar 合并引擎（whiteout / opaque 语义）
  oci.py        OCI 布局读写原语（摘要寻址、原子写入）
tests/          pytest：合并语义、API、失败原子性、内存上界
examples/
  make_sample_image.py  构造三层示例镜像（顶层含 opaque + 新增文件）
  verify_equal.py       展开两个标签的文件树并逐路径比对
  demo.sh               一键演示（Docker Compose 或 DIRECT=1 直跑）
Dockerfile / docker-compose.yml
```

## 运行

```bash
# 准备示例镜像（三层未压缩 tar，标签 demo:1.0.1）
python3 examples/make_sample_image.py --force oci-layout

# 启动服务（Docker Compose，监听 8080）
docker compose up -d --build

# 合并末两层：demo:1.0.1 -> demo:1.0.1-squashed
curl -X POST http://localhost:8080/merge \
  -H 'Content-Type: application/json' \
  -d '{"source_tag":"demo:1.0.1","target_tag":"demo:1.0.1-squashed"}'

# 校验新旧镜像展开后完全一致（路径/内容/权限/属主）
python3 examples/verify_equal.py oci-layout demo:1.0.1 demo:1.0.1-squashed
```

没有 Docker 的环境可以直跑：`DIRECT=1 examples/demo.sh`（一键完成上述全部步骤）。

## API

### `POST /merge`

请求：

```json
{"source_tag": "demo:1.0.1", "target_tag": "demo:1.0.1-squashed"}
```

响应 `200`：

```json
{
  "source_tag": "demo:1.0.1",
  "target_tag": "demo:1.0.1-squashed",
  "manifest_digest": "sha256:…",
  "config_digest": "sha256:…",
  "merged_layer": {"digest": "sha256:…", "diff_id": "sha256:…", "size": 2048,
                   "mediaType": "application/vnd.oci.image.layer.v1.tar"},
  "layers": ["sha256:…(base)", "sha256:…(merged)"],
  "base_layers_kept": 1,
  "layers_merged": 2
}
```

错误码：`400` 原新标签相同；`404` 原标签不存在；`409` 新标签已存在；
`422` 不支持的输入（多平台、层数不足、层被压缩、非法 tar 条目等）；`500` 布局损坏等。

### `GET /tags` / `GET /healthz`

列出布局中的标签 / 健康检查。

## 演示场景说明

示例镜像除了 opaque 遮蔽场景外，还用 `app/.config` 与 `app/replaced` 覆盖三类目录重建：

- `app/data/.wh..wh..opq`（遮蔽 base 与 patch1 写入 `/app/data` 的全部内容）以及同层新增的
  `app/data/fresh.txt`；
- `app/.config`：patch1 用普通 whiteout 删掉整个基础层目录，patch2 在原路径显式重建
  （`0750 1000:1000`）并新增 `runtime.ini`，且同层 whiteout 条目在 tar 流中排在新条目**之后**；
- `app/replaced`：基础层是含 `leaf.txt` 的目录，patch1 把它换成普通文件，patch2 再重建为目录
  并新增 `new.txt`——该路径输入中没有 whiteout，合并层会**合成**一个零字节 whiteout 来持续隐藏基础层旧子项。

合并后：

- 合并层仍含 `app/data/.wh..wh..opq` —— 遮蔽继续作用于基础层；
- 合并层含 `app/.wh..config`（保留）与 `app/.wh.replaced`（合成），两个重建目录的基础层旧子项
  （`base.ini`、`settings.ini`、`leaf.txt`）都不会复活；
- `app/.config/runtime.ini`、`app/replaced/new.txt` 等新条目及其权限/属主原样保留；
- 普通目录叠加（如 `etc/`）未被改动的低层子项 `etc/keep.cfg` 继续可见；
- `etc/.wh.old.cfg` 的删除语义保留（即使 base 里也有该文件，展开结果仍一致）。

合并层 tar 的条目顺序保证 whiteout 在其作用的目标子树之前，例如：

```
app/.wh..config                 # 先删除低层目录
app/.config/                    # 再重建目录
app/.config/runtime.ini
```

`examples/verify_equal.py` 会把新旧两个标签的所有层分别展开到内存虚拟文件系统，
逐路径比对类型、内容摘要、权限位与 uid/gid。

## 测试

```bash
python3 -m pytest        # 需要 fastapi/uvicorn/pytest/httpx
```

覆盖：opaque 遮蔽后新增、whiteout 各类组合、目录重建三场景（低层 whiteout 后高层重建、
低层文件换目录后高层重建、同层 whiteout 排在重建条目之后）、普通目录叠加保留低层子项、
元数据/属主保留、32MiB 文件的内存上界（<16MiB）、API 全流程、失败时不发布新标签且原标签可用、
临时文件清理等。

## 限制

- 仅单平台镜像；多平台（manifest list）会被 422 拒绝。
- 末两层必须是未压缩 tar（gzip/zstd 层会被拒绝，含魔数嗅探）。
- 层内只支持普通文件、目录与两种 whiteout。
- 单进程内用锁串行化合并；多副本部署需自行协调对同一布局的并发写入。
