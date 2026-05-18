#!/bin/bash

# 配置
WEBVIEW_URL="http://localhost:8010"
PILOT_URL="http://localhost:8020"

echo "=== 1. 创建两个同名的物体 ==="
NAME="MultiTest"
# 创建第一个
curl -s "$WEBVIEW_URL/api/add?name=$NAME" > /dev/null
# 创建第二个
curl -s "$WEBVIEW_URL/api/add?name=$NAME" > /dev/null

echo "已创建两个名为 '$NAME' 的物体。"
echo ""

echo "=== 2. 向 AI Pilot 发送控制指令 ==="
COMMAND="请让 $NAME 把航速提升至 450 节，爬升到 35000 英尺。"
echo "发送指令: \"$COMMAND\""

# 调用 AI Pilot 的 radio_RX 接口
PILOT_RESPONSE=$(curl -s -X POST "$PILOT_URL/radio_RX" \
     -H "Content-Type: application/json" \
     -d "{\"text\": \"$COMMAND\"}")

echo "AI Pilot 返回结果:"
echo "$PILOT_RESPONSE" | python3 -m json.tool

echo ""
echo "=== 3. 验证 webview 中的状态 ==="
VERIFY_RESPONSE=$(curl -s "$WEBVIEW_URL/api/objects")
echo "当前所有物体状态:"
echo "$VERIFY_RESPONSE" | python3 -m json.tool

echo ""
echo "测试完成！如果一切正常，您应该能看到所有名为 '$NAME' 的物体都已被更新。"
