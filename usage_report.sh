#!/bin/bash
# usage_report.sh : who used what on seekal.fun (reads usage_log.csv)
cd "$(dirname "$0")"
[ -f usage_log.csv ] || { echo "no usage yet (usage_log.csv not created)"; exit; }
echo "== uses per tool, all time";            tail -n +2 usage_log.csv | cut -d, -f2 | sort | uniq -c
echo; echo "== unique visitors per tool";     tail -n +2 usage_log.csv | cut -d, -f2,3 | sort -u | cut -d, -f1 | uniq -c
echo; echo "== uses per day (last 14 days)";  tail -n +2 usage_log.csv | awk -F, '{print substr($1,1,10), $2}' | sort | uniq -c | tail -28
echo; echo "== last 10 Token Intel searches"; grep ',token_intel,' usage_log.csv | tail -10 | cut -d, -f1,4
