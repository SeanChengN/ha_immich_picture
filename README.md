# Immich Picture – Home Assistant 集成

简体中文 | [English](README.en.md)

将 [Immich](https://immich.app) 相册变成 Home Assistant 中自动轮播的媒体 `camera` 实体。可用于图片卡、图片概览卡和内置网页播放器，照片与视频会按设定间隔切换。

---

## 功能

- 五种来源：随机、全部（近期）、相册、收藏、元数据搜索
- 随机和搜索支持 Immich JSON 筛选，可按人物、城市、日期、媒体类型等过滤
- 轮播间隔可设为 5 秒至 1 小时；媒体池刷新间隔可设为 1 分钟至 24 小时
- 缩略图缓存具备离线回退；小于 50 MiB 的视频可缓存为本地 MP4 播放
- 同一 Immich 账户可创建多个独立来源，完全相同的来源也允许重复添加
- 竖图和方图会两两横向合成；视频在 camera 中显示 JPEG 预览，在网页播放器中循环播放
- 可在“配置”中修改数量、时间和 JSON 筛选，无需删除条目

---

## 要求与权限

- Home Assistant 2023.x 或更高版本
- Home Assistant 主机可访问的 Immich 实例（局域网或 VPN）
- Immich API Key：在 **账户设置 → API Keys** 创建

为使用全部五种来源，API Key 应拥有：`asset.read`（随机、近期、收藏和搜索）、`album.read`（相册）与 `asset.view`（查看媒体）权限。

---

## 安装

### HACS（推荐）

1. 打开 HACS → **集成** → ⋮ → **自定义仓库**。
2. 添加本仓库地址，类别选择 **Integration**。
3. 搜索并安装 **Immich Picture**，然后重启 Home Assistant。

### 手动安装

1. 将 `custom_components/immich_picture/` 复制到 Home Assistant 配置目录的 `custom_components/` 下。
2. 重启 Home Assistant。

---

## 配置与多来源

1. 进入 **设置 → 设备与服务 → 添加集成**，搜索 **Immich Picture**。
2. 选择已保存的 Immich 账户，或输入 Immich URL（例如 `http://192.168.1.100:2283`）和 API Key。
3. 选择来源，设置来源专属参数、轮播间隔与刷新间隔。
4. 集成会创建可直接放入仪表盘的 `camera` 实体。

再次执行“添加集成”即可为相同账户增加更多来源。选择已保存账户会复用凭据，但每个来源的实体、缓存、轮播时间和播放器 URL 都是独立的。

## 媒体来源

### 随机媒体

每次刷新通过 `POST /api/search/random` 获取随机图片和视频预览，可选 JSON 筛选：

```json
{
  "personIds": ["uuid-of-person-1", "uuid-of-person-2"],
  "country": "Japan",
  "type": "VIDEO"
}
```

[Immich API 参考](https://api.immich.app/endpoints/search/searchRandom)

### 全部媒体（近期）

按从新到旧或从旧到新的顺序获取媒体库中的资产。

### 相册媒体

从选定相册获取图片和视频预览；配置时会从 Immich 加载相册供选择。

### 收藏媒体

仅获取在 Immich 中标记为收藏的图片和视频预览。

### 按元数据搜索

通过 `POST /api/search/metadata` 使用 JSON 查询。可按城市、国家、日期、相机、归档状态、收藏、人物及媒体类型过滤：

```json
{
  "city": "Paris",
  "isFavorite": true,
  "type": "VIDEO",
  "takenAfter": "2023-01-01T00:00:00Z"
}
```

[Immich API 参考](https://api.immich.app/endpoints/search/searchAssets)

## 可配置选项

| 设置 | 说明 | 范围 |
|---|---|---|
| 媒体轮播间隔 | 当前画面切换的频率 | 5–3600 秒 |
| API 刷新间隔 | 从 Immich 获取新媒体池的频率 | 60–86400 秒 |
| 媒体数量 | 每次刷新载入的资产数量 | 1–500 |
| 筛选（JSON） | 随机和搜索来源的 API 请求体 | 任意有效 JSON 对象 |

保存配置后集成会自动重载，改动立即生效。

---

## 缓存与离线回退

成功下载的图片与视频预览会按 Immich 资产 ID 写入 JPEG；竖图组合使用组合轮播 ID。严格小于 50 MiB 的视频还会缓存为 MP4：

```
<ha-config-dir>/immich_picture/image_cache/<entry-id>/<asset-id>.jpg
<ha-config-dir>/immich_picture/image_cache/<entry-id>/<asset-id>.mp4
```

JPEG、MP4 和中断下载的临时文件只保留当前媒体池仍包含的资产，下一次刷新会清除过期文件。每个配置项的 MP4 缓存最多 **500 MiB**，超过时按最近最少使用原则清理，同时保护当前播放和下载中的文件。升级前的旧序号 JPEG 会作为启动回退保留，首次成功写入新格式缓存后自动清理。

Immich 可用时会获取最新预览；播放器优先读取本地 MP4，不满足大小条件或大小未知时则代理 Immich 播放。Immich 离线时，只会回退到对应资产的缓存；若该资产没有缓存，会继续保留上一张已显示的画面。删除配置项会删除该条目的全部 JPEG、MP4 和临时缓存；手动删除缓存目录也安全，会在下次成功获取时重建。

---

## 仪表盘与实体属性

将 `camera.immich_picture_slideshow_*` 放入 **图片** 或 **图片概览**卡即可自动更新。实体还提供以下属性供自动化或模板使用：

| 属性 | 含义 |
|---|---|
| `asset_id` | 当前 Immich 资产 UUID |
| `asset_type` | 当前类型：`IMAGE` 或 `VIDEO` |
| `filename` | 原始文件名 |
| `taken_at` | 拍摄时间 |
| `total_assets` | 当前媒体池总数 |
| `current_index` | 当前媒体的一位序号 |
| `endpoint` | 当前来源标识 |
| `player_url` | 已鉴权的内置媒体播放器地址 |

每个设备还会创建诊断传感器 `sensor.immich_picture_image_cache_path`，用于显示该实例的缓存目录。

---

## 视频网页播放器

camera 实体始终返回 JPEG 快照，VIDEO 资产也一样。若需播放视频，请使用 **网页** 卡，并使用 camera 属性中的完整 `player_url` 作为 URL。播放器与轮播保持同步：图片显示快照，视频静音循环播放，直到正常轮播时间到达。

```yaml
type: iframe
url: /api/immich_picture/player/<entry-id>?token=<player-token>
aspect_ratio: "16:9"
allow: autoplay; fullscreen
disable_sandbox: true
```

应直接复制完整 `player_url`（包括 `token`），不要手工拼接。该 token 是仅限当前播放器媒体池的能力 URL；播放器会代理 Immich 视频请求，不会暴露 API Key。请把它视为机密，不要经未认证的反向代理公开。为满足浏览器自动播放规则，视频默认静音；与控件交互后可开启声音。

如果 URL 泄露，可在 **开发者工具 → 操作** 调用 `immich_picture.rotate_player_token`。提供 `entry_id` 可只轮换一个轮播；留空则轮换全部播放器。旧 URL 会立即失效，随后将实体属性中新生成的 `player_url` 更新到网页卡。

---

## 故障排除

**启动时没有图片**：确认 Home Assistant 可访问 Immich URL，并在 **设置 → 系统 → 日志** 中按 `immich_picture` 筛选。

**画面不更新但仍显示照片**：Immich 可能暂时不可用，当前正在使用缓存；恢复连接后会自动继续获取最新媒体。

**配置时无法连接**：确认 URL 包含端口（如 `http://192.168.1.100:2283`）且 API Key 正确。

**API Key 已过期或撤销**：Immich 返回 `401` 时 Home Assistant 会显示重新认证表单。更新一次 API Key 后，使用旧凭据的全部来源都会一并更新并重载。

**JSON 筛选被拒绝**：字段必须是 JSON 对象而不是数组，粘贴前可使用 [jsonlint.com](https://jsonlint.com) 验证。

---

## 更新日志

### 1.7.1

- **Hassfest 修复**：补齐 HTTP 依赖、仅 Config Entry 架构及符合规范的翻译结构。
- **文档语言**：简体中文现在是默认 README；完整英文文档见 [README.en.md](README.en.md)。

### 1.7.0

- **视频缓存限制**：每个轮播只允许一个下载任务，MP4 使用 500 MiB 的 LRU 缓存预算。
- **更安全的播放器 URL**：新增 token 轮换、严格响应头及页面过期请求保护。
- **更可靠的 API 访问**：增加请求超时、有限重试和相册数量限制。
- **工程自动化**：新增测试、CI 校验与 tag 自动 GitHub Release。

### 1.6.0

- **可靠媒体缓存**：JPEG 使用资产 ID 和原子写入，避免旧轮播槽位显示为其他资产。
- **低内存视频缓存**：短视频分块写盘，并在重载或删除时安全取消。
- **平滑播放器同步**：播放器按计划轮播时间刷新，不再每秒轮询。
- **已保存账户优化**：合并重复账户选项，重新认证会更新全部匹配来源。

### 1.5.1

- 新增简体中文配置、选项、错误和缓存路径传感器翻译。

### 1.5.0

- 小于 50 MiB 的视频可本地缓存播放，并随来源资产池或配置项删除而清理。
- 同账户可复用凭据创建多个独立来源。

### 1.4.0

- 新增图片/视频混合轮播的内置 HTML5 播放器；视频静音循环至正常轮播切换。

### 1.3.2

- VIDEO 资产与 IMAGE 一起进入轮播，并显示 Immich 生成的 JPEG 预览。

### 1.3.0

- 竖图和方图会两两横向合成；横图保持原样，落单竖图不显示。

### 1.2.1

- 首个跟踪版本。
