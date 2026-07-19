#!/bin/bash
# 转换注册机账号到 CPA

ACCOUNTS_FILE="$1"
CPA_DIR="$HOME/.cli-proxy-api"
PROXY="http://127.0.0.1:7892"

if [ -z "$ACCOUNTS_FILE" ]; then
    echo "用法: $0 <账号文件>"
    echo "示例: $0 ~/grokRegister-cpa/accounts_20260711_231512.txt"
    exit 1
fi

if [ ! -f "$ACCOUNTS_FILE" ]; then
    echo "文件不存在: $ACCOUNTS_FILE"
    exit 1
fi

echo "读取账号文件: $ACCOUNTS_FILE"
echo "认证目录: $CPA_DIR"
echo "代理: $PROXY"
echo ""

# 逐行处理
while IFS= read -r line; do
    if [ -z "$line" ]; then
        continue
    fi

    echo "处理: $line"

    # 写入临时文件
    echo "$line" > /tmp/sso_temp.txt

    # 转换并上传
    cd ~/grokRegister-cpa && python3 sso_to_auth_json.py \
        --sso /tmp/sso_temp.txt \
        --cpa-auth-dir "$CPA_DIR" \
        --proxy "$PROXY"

    echo "---"
done < "$ACCOUNTS_FILE"

echo ""
echo "完成！认证文件已保存到: $CPA_DIR"
ls -la "$CPA_DIR"/xai-*.json 2>/dev/null | wc -l
echo "个认证文件"
