# 你的清华SIGS — AI 校园设计平台

基于 Flask 的全栈 Web 应用，集成 AI 图像生成、360° 全景浏览、校园地图探索和创意设计社区。

## 项目结构

```
image_test/
├── app.py                  # Flask 主程序（所有路由 + 业务逻辑）
├── config.py               # 全局配置（API 密钥、常量）
├── requirements.txt        # Python 依赖
├── .env / .env.example     # 环境变量（SUCHUANG_API_KEY）
├── static/                 # 上传/转换/生成的图片文件
│   ├── uploads/            #   用户上传的参考图
│   ├── results/            #   AI 生成结果（本地缓存）
│   ├── converted/          #   格式转换后的 JPG
│   └── panorama/           #   用户上传的全景图
├── panorama_images/        # 清华 SIGS 校园实景照片（166 张全景图）
│   ├── coordinates.csv     #   每张全景图的 GPS 坐标 (uuid,lat,lon,alt)
│   └── panorama_images_*/  #   按采集批次分目录存放
└── templates/              # 前端页面（全部内联 CSS/JS，无构建工具）
    ├── index.html          #   [模块1] GPT-Image-2 图生图工具
    ├── nano.html           #   [模块2] NanoBanana2 4K 超分工具
    ├── convert.html        #   [模块3] PNG→JPG 格式转换
    ├── panorama.html       #   [模块4] 360° 全景单图查看器
    ├── map.html            #   [模块5] 校园实景地图（Leaflet + Three.js）
    └── sigs.html           #   [主应用] "你的清华SIGS" 完整游戏化 SPA
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API 密钥
cp .env.example .env
# 编辑 .env：填入速创 API 密钥（在 https://api.wuyinkeji.com/user/key 获取）
# SUCHUANG_API_KEY=你的密钥

# 3. 启动服务器
python app.py
# 访问 http://127.0.0.1:5000
```

## 后端 API 清单

### 速创 API 对接（配置在 config.py）

应用对接了速创平台的两个 AI 图像模型：

| 模型 | 接口 | 用途 |
|------|------|------|
| GPT-Image-2 | `POST /api/async/image_gpt` | 图生图：参考图 + 提示词 → 生成新全景图 |
| NanoBanana2 | `POST /api/async/image_nanoBanana2` | 超分：1K/2K/4K 分辨率，最多 14 张参考图 |
| 通用查询 | `GET /api/async/detail?id=xxx` | 查询异步任务结果 |

两个模型都是异步模式：提交返回 `task_id` → 轮询获取结果。状态码 `2` 表示完成，`3` 表示失败。

### 自建 API 路由

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/generate` | POST | 提交 GPT-Image-2 生图任务（FormData: prompt + file + size） |
| `/api/result/<task_id>` | GET | 轮询生图结果，自动下载 PNG 并转为 JPG 返回 |
| `/api/nano/generate` | POST | 提交 NanoBanana2 超分任务（FormData: prompt + file + resolution + aspect_ratio） |
| `/api/nano/result/<task_id>` | GET | 轮询超分结果，自动转 JPG |
| `/api/upload` | POST | 上传参考图，返回 base64 data URL |
| `/api/convert` | POST | JSON: `{"url":"图片URL"}` → 下载并转为 JPG |
| `/api/panorama/upload` | POST | 上传全景图，返回本地 URL |
| `/api/map/points` | GET | 返回 166 个校园实景点的 `[{id, lat, lon, image_url}]` |
| `/panorama_images/<path>` | GET | 直接访问全景图片文件 |

### 关键设计决策

- **图片中转**：GPT-Image-2 和 NanoBanana2 的 `urls` 参数只接受 HTTP URL。本地文件通过 `tmpfiles.org`（免费图床，~1小时有效期）临时中转。
- **Base64 支持**：NanoBanana2 文档标注支持 Base64，实际测试仅小图可用（< 500KB），大图仍走图床中转。
- **PNG→JPG 自动转换**：生成结果接口自动调用 `_convert_to_jpg()`，处理 RGBA→RGB（白底合成），避免 PNG 透明通道问题。
- **异步轮询**：前端每 5 秒轮询一次结果，4K 超分约需 60-80 秒。

## 前端页面架构

### 模块独立性

每个 `.html` 文件都是完全自包含的独立页面（内联 CSS/JS），可以单独运行和测试：

| 页面 | 路由 | 依赖 |
|------|------|------|
| `index.html` | `/` | 无外部依赖 |
| `nano.html` | `/nano` | 无外部依赖 |
| `convert.html` | `/convert` | 无外部依赖 |
| `panorama.html` | `/panorama` | Three.js (importmap CDN) |
| `map.html` | `/map` | Leaflet + Three.js (CDN) |
| `sigs.html` | `/sigs` | Leaflet + Three.js (CDN) |

### Three.js 全景查看器（可复用组件）

`panorama.html` 和 `map.html` 都使用相同的 Three.js 全景渲染模式：

- `SphereGeometry(50, 128, 64)` + `MeshBasicMaterial({side: BackSide})`
- 球心放置 `PerspectiveCamera`，纹理贴在球体内侧
- 鼠标左键拖拽旋转（`lon += dx * 0.003`），滚轮缩放 FOV
- 纹理翻转修正：`texture.wrapS = RepeatWrapping; texture.repeat.x = -1`（修正 BackSide 镜像）
- 3D 导航精灵（Sprite）：Haversine 公式计算附近点方位，放置在地面高度（-25° 仰角）
- 动画循环中 `state.autoRotate` 控制自动旋转（速度 0.0015 rad/frame）

### SPA 主应用（`sigs.html`）

基于 hash 路由的单页应用，状态管理用全局 `window.state` 对象：

```
#welcome → 欢迎页 + 语言设置（CN/EN）
#map     → 地图选点 + 全景漫游/设计编辑双模式
#community → 设计社区画廊 + 3D 全景查看 + 点赞
```

**关键架构决策**：
- 普通脚本定义全局状态和业务逻辑，Module 脚本负责 Three.js 渲染
- `let/const` 变量在 Module 中不可见 → 通过 `window.state` / `window.allPoints` 透传
- 全景 UI 元素从 `#page-map` 内移到 `<body>` 直接子元素，避免 position:fixed 被父级层叠上下文困住
- 所有文本通过 `t(key)` 函数 + `I18N` 对象实现中英双语
- 社区数据存在 `localStorage`（key: `sigs_community_posts` / `sigs_user_likes`）

### AI 设计 Agent 提示词生成流程

4 轮逐级引导选择 → 编译英文提示词：

```
Round 1: 风格 (8选1) → Round 2: 色调 (6选1) → Round 3: 元素 (8选1) → Round 4: 氛围 (8选1)
                                                                              ↓
"Transform this equirectangular 360-degree panorama photo.
 Apply {style}. Use {color}. Feature {elements}. Create a {atmosphere}.
 Preserve the 360-degree equirectangular projection format..."
```

提交到 `/api/generate`，轮询完成 → 全景视图更新为 AI 生成图 → 可选择继续修改或分享。

### 分享→社区→4K 超分流程

1. 确认分享 → 调用 `/api/nano/generate`（resolution=4K）
2. 轮询超分结果 → 保存 post 对象到 localStorage
3. 跳转 `#community`，卡片展示缩略图+提示词+点赞数
4. 点击卡片 → 全屏 3D 全景查看（复用 Three.js 社区专用渲染器）
5. 点赞/取消（localStorage 持久化）

## 校园全景数据集

- **位置**：`panorama_images/`
- **规模**：166 张等距柱状投影全景图（4 个采集批次）
- **坐标**：`coordinates.csv` 记录每张图的 GPS（纬度/经度/海拔）
- **命名规则**：`{SET_ID}_{NUM}` → 文件路径 `panorama_images_{SET_ID}/{NUM}.jpg`
- **地图服务**：`/api/map/points` 解析 CSV → 校验文件存在 → 返回 JSON

## 扩展指南

### 新增语言

1. 在 `sigs.html` 的 `I18N` 对象中添加新语言子对象（如 `jp: {}`）
2. 补全所有 key 的翻译
3. 在 Settings 面板中添加语言选项
4. 在 `setLang()` 函数中处理

### 新增 AI 模型

1. 在 `config.py` 添加模型接口 URL 和参数常量
2. 在 `app.py` 添加 `_submit_xxx()` 辅助函数和 `/api/xxx/generate` 路由
3. 复用已有的 `_convert_to_jpg()`、`_upload_to_hosting()`、`_query_result()` 工具函数

### 新增实景点

1. 将全景图放入 `panorama_images/` 对应子目录
2. 在 `coordinates.csv` 中添加一行：`SETID_NUM,lat,lon,alt`

### 常见陷阱

- **Module 脚本作用域隔离**：`<script type="module">` 无法访问普通脚本的 `let/const`，需要显式 `window.xxx = xxx` 透传
- **position:fixed + 父级 stacking context**：父元素有 `position:fixed; z-index` 时会困住子元素的 `position:fixed`，解决方法是移到 `<body>` 直接子级
- **tmpfiles.org 有效期**：图床文件约 1 小时后过期，社区帖子的 URL 指向本地 `/static/converted/` 不会过期
- **Leaflet 容器尺寸**：创建 Map 前必须确保容器 `position:absolute; inset:0` 且有明确尺寸，否则渲染空白
- **BackSide 镜像**：球体内侧贴图会有水平翻转，需用 `texture.repeat.x = -1` 修正
- **Base64 大图限制**：NanoBanana2 的 base64 约 >500KB 即报 500，大图须走图床 URL
