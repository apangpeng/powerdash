#!/bin/bash

# index.cgi — fnOS CGI 入口脚本（iframe 同源接入方式，与 wol / xunlei 同款）
# 由飞牛自带 nginx 在 /cgi/ThirdParty/powerdash/index.cgi/ 下以 CGI 方式执行。
#
# v1.3.1 起改为「全量反向代理」：
#   - 页面、数据（data.js）、接口（/api/*）全部转发到本机后端（127.0.0.1:8776）
#   - 转发浏览器在本域持有的 Cookie（访问口令会话），后端才能识别会话
#   - 回传后端的 Content-Type / Set-Cookie / Location（Location 改写到 CGI 基址），
#     保证「设置/验证访问口令 → 发会话 Cookie → 数据可读」整条链路在内嵌界面可用
#   - 后端未启动时，页面类请求退回包内静态文件，接口类请求返回 502 JSON
#
# 修复内容（v1.3.1）：
#   1. 之前不转发 Cookie、不回传 Set-Cookie，内嵌界面永远过不了访问口令，
#      data.js 反代回来的是 302 空响应，页面误报「缺少 data.js」。
#   2. 之前反代响应 Content-Type 被写死为 application/json，data.js 会被浏览器
#      以 MIME 类型不匹配拒绝执行（即便有会话也一样加载失败）。
#   3. 之前不回传 Location，未验证访问口令时无法自动跳到口令页。

# ========== 配置 ==========

# 静态文件根目录（后端未启动时的回退目录）
BASE_PATH="/var/apps/powerdash/target/ui"

# 后端端口：从 manifest 的 service_port 动态读取，默认 8776
MANIFEST_FILE="/var/apps/powerdash/manifest"
BACKEND_PORT="8776"
if [ -f "$MANIFEST_FILE" ]; then
    _port=$(grep -E '^service_port[[:space:]]*=' "$MANIFEST_FILE" | awk -F '=' '{print $2}' | tr -d ' \r')
    if [ -n "$_port" ]; then
        BACKEND_PORT="$_port"
    fi
fi
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"

# ========== 解析请求路径 ==========

URI_NO_QUERY="${REQUEST_URI%%\?*}"
REL_PATH="/"
CGI_BASE="/cgi/ThirdParty/powerdash/index.cgi"
case "$URI_NO_QUERY" in
    *index.cgi*)
        REL_PATH="${URI_NO_QUERY#*index.cgi}"
        CGI_BASE="${URI_NO_QUERY%%index.cgi*}index.cgi"
        ;;
esac
[ -z "$REL_PATH" ] && REL_PATH="/"
[ "$REL_PATH" = "/" ] && REL_PATH="/index.html"

TARGET_URL="${BACKEND_URL}${REL_PATH}"
if [ -n "$QUERY_STRING" ]; then
    TARGET_URL="${TARGET_URL}?${QUERY_STRING}"
fi

# ========== 转发到后端 ==========

# 转发浏览器在本域（飞牛门户）持有的 Cookie，后端才能识别访问口令会话
COOKIE_HDRS=()
if [ -n "$HTTP_COOKIE" ]; then
    COOKIE_HDRS=(-H "Cookie: ${HTTP_COOKIE}")
fi

BODY_FILE=$(mktemp)
HEAD_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE" "$HEAD_FILE"' EXIT

case "$REQUEST_METHOD" in
    POST|PUT)
        POST_DATA=""
        if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
            POST_DATA=$(dd bs=1 count="$CONTENT_LENGTH" 2>/dev/null)
        fi
        HTTP_CODE=$(curl -s -o "$BODY_FILE" -D "$HEAD_FILE" -w '%{http_code}' -X "$REQUEST_METHOD" \
            -H "Content-Type: ${CONTENT_TYPE:-application/json}" \
            "${COOKIE_HDRS[@]}" \
            --data-binary "$POST_DATA" \
            "$TARGET_URL" 2>/dev/null)
        ;;
    DELETE)
        HTTP_CODE=$(curl -s -o "$BODY_FILE" -D "$HEAD_FILE" -w '%{http_code}' -X DELETE \
            "${COOKIE_HDRS[@]}" \
            "$TARGET_URL" 2>/dev/null)
        ;;
    *)
        HTTP_CODE=$(curl -s -o "$BODY_FILE" -D "$HEAD_FILE" -w '%{http_code}' \
            "${COOKIE_HDRS[@]}" \
            "$TARGET_URL" 2>/dev/null)
        ;;
esac

# ========== 后端不可用回退 ==========

case "$HTTP_CODE" in
    [1-5][0-9][0-9])
        ;;
    *)
        # 后端未启动（curl 返回 000 等）：页面类请求退回包内静态文件，接口返回 502
        TARGET_FILE="${BASE_PATH}${REL_PATH}"
        case "$TARGET_FILE" in
            *..*) TARGET_FILE="" ;;
        esac
        if [ -n "$TARGET_FILE" ] && [ -f "$TARGET_FILE" ]; then
            ext="${TARGET_FILE##*.}"
            case "$ext" in
                html|htm) mime="text/html; charset=utf-8" ;;
                css)      mime="text/css; charset=utf-8" ;;
                js)       mime="application/javascript; charset=utf-8" ;;
                json)     mime="application/json; charset=utf-8" ;;
                png)      mime="image/png" ;;
                jpg|jpeg) mime="image/jpeg" ;;
                gif)      mime="image/gif" ;;
                svg)      mime="image/svg+xml" ;;
                ico)      mime="image/x-icon" ;;
                woff2)    mime="font/woff2" ;;
                woff)     mime="font/woff" ;;
                txt|log)  mime="text/plain; charset=utf-8" ;;
                *)        mime="application/octet-stream" ;;
            esac
            echo "Status: 200 OK"
            echo "Content-Type: $mime"
            echo "Cache-Control: no-cache"
            echo ""
            cat "$TARGET_FILE"
            exit 0
        fi
        echo "Status: 502"
        echo "Content-Type: application/json; charset=utf-8"
        echo "Cache-Control: no-store"
        echo ""
        echo '{"error": "用电总览服务未运行，请在应用中心启动应用"}'
        exit 0
        ;;
esac

# ========== 回传后端响应 ==========

echo "Status: $HTTP_CODE"

CT=$(grep -i '^Content-Type:' "$HEAD_FILE" | head -n 1 | sed 's/^[Cc]ontent-[Tt]ype:[[:space:]]*//' | tr -d '\r')
if [ -n "$CT" ]; then
    echo "Content-Type: $CT"
else
    echo "Content-Type: application/json; charset=utf-8"
fi

# 会话 Cookie 必须原样回传，访问口令验证才能生效
grep -i '^Set-Cookie:' "$HEAD_FILE" | while IFS= read -r _line; do
    printf '%s\n' "$_line" | tr -d '\r'
done

# 重定向地址改写到本 CGI 基址：/access.html -> /cgi/ThirdParty/powerdash/index.cgi/access.html
LOC=$(grep -i '^Location:' "$HEAD_FILE" | head -n 1 | sed 's/^[Ll]ocation:[[:space:]]*//' | tr -d '\r')
if [ -n "$LOC" ]; then
    case "$LOC" in
        /*) LOC="${CGI_BASE}${LOC}" ;;
    esac
    echo "Location: $LOC"
fi

echo "Cache-Control: no-store"
echo ""
cat "$BODY_FILE"
exit 0
