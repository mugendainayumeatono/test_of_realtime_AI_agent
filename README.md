# test_of_realtime_AI_agent

## 实时地图物体 Web 应用

本项目使用 `FastAPI` 构建后端，前端基于 `Leaflet` 地图实现实时多物体位置展示。

### 功能

- 点击地图放置物体
- 记录物体 GPS 坐标
- 实时随机移动物体坐标
- 可设置移动速度（节）
- 可设置高度（英寸）
- 可设置垂直速度（英寸/秒）
- 支持多个物体同时存在

### 运行步骤

本地开发推荐使用 `uv` 管理依赖：

```bash
python3.11 -m pip install uv
cd src
uv sync
uv run uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

然后打开浏览器访问 `http://localhost:8000`。

### 文件结构

- `pyproject.toml`：项目依赖配置，用于 `uv` 管理包
- `src/app.py`：FastAPI 后端和 WebSocket 实时更新逻辑
- `src/templates/index.html`：前端页面模板
- `src/static/app.js`：前端地图与 WebSocket 客户端逻辑
- `src/requirements.txt`：兼容旧方式的依赖列表
- `Dockerfile`：Docker 容器构建配置
- `.dockerignore`：Docker 构建排除规则

## Docker 容器运行

构建镜像：

```bash
docker build -t realtime-map-app .
```

运行容器并将本地 `src` 目录挂载到容器中：

```bash
docker run --rm -p 8000:8000 -v "$PWD/src":/app/src realtime-map-app
```

使用 Docker Compose 运行：

```bash
docker compose up --build
```

Compose 配置会将本地 `src` 目录映射到容器 `/app/src`，并以 `uvicorn --reload` 启动服务，因此本地代码修改可以实时生效。

然后在浏览器访问：

`http://localhost:8000`

## Docker 容器运行

构建镜像：

```bash
docker build -t realtime-map-app .
```

运行容器：

```bash
docker run --rm -p 8000:8000 realtime-map-app
```

然后在浏览器访问：

`http://localhost:8000`
