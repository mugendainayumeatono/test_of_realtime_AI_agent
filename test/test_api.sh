#!/bin/bash

# API 基础地址 (根据 docker-compose.yml 映射的端口)
BASE_URL="http://localhost:8010"

echo "=== 1. 创建新物体 ==="
# 调用 api/add 接口
# 使用 -s 静默模式，并将结果存入变量
RESPONSE=$(curl -s "$BASE_URL/api/add?name=TestObject&speed=15&altitude=1000")

echo "接口返回: $RESPONSE"

# 提取返回结果中的 ID (使用简单的 grep 和 sed，避免依赖 jq)
OBJ_ID=$(echo $RESPONSE | grep -o '"id":"[^"]*' | sed 's/"id":"//')

if [ -z "$OBJ_ID" ]; then
    echo "错误: 无法获取物体 ID，请确保服务已启动。"
    exit 1
fi

echo "成功创建物体! ID 为: $OBJ_ID"
echo ""
echo "请在浏览器中确认物体已出现，然后按任意键继续测试修改功能..."
read -n 1 -s

echo "=== 2. 修改物体参数 ==="
echo "正在修改 ID 为 $OBJ_ID 的物体: 航速 -> 50, 目标航向 -> 180, 目标高度 -> 5000"

# 调用 api/update 接口
UPDATE_RESPONSE=$(curl -s "$BASE_URL/api/update?id=$OBJ_ID&speed=50&heading=180&altitude=5000")

echo "接口返回: $UPDATE_RESPONSE"
echo ""
echo "测试完成！请查看地图观察物体的变化（它应该开始转向并爬升）。"
