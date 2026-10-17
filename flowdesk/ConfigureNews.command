#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
printf 'FlowDesk 新闻接口只读配置。秘钥只保存到本机 .private/news.env（权限 600）。\n'
printf '1 = Benzinga 实时新闻（需 Massive Benzinga News 授权）；2 = Massive 普通新闻（按小时更新）；3 = Finnhub（是否实时由套餐决定）\n'
read -r -p '请选择 1/2/3: ' choice
case "$choice" in
  1|2) var='MASSIVE_API_KEY' ;;
  3) var='FINNHUB_API_KEY' ;;
  *) echo '选择不正确'; exit 2 ;;
esac
IFS= read -r -s -p "请粘贴 ${var}（不会显示）：" value
printf '\n'
if ! [[ "$value" =~ ^[A-Za-z0-9._-]{8,256}$ ]];then echo '密钥格式不合法或为空';exit 2;fi
mkdir -p .private
if [ -L .private ] || [ -L .private/news.env ];then echo '拒绝符号链接';exit 2;fi
chmod 700 .private
# Existing other provider key (if any) is not preserved; configure one provider at a time.
printf '%s=%s\n' "$var" "$value" > .private/news.env
chmod 600 .private/news.env
unset value
printf '本地密钥已设置。请执行 bash PollNews.command 或 bash StartNews.command。\n'
