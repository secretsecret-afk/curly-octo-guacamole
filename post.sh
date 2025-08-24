#!/bin/bash

SESSION_NAME="post_session"
SCRIPT_PATH="$HOME/tg-bots/rkk/post.py"

tmux new-session -d -s $SESSION_NAME "python3 $SCRIPT_PATH"

echo "Скрипт post.py запущен в tmux сессии '$SESSION_NAME'."
echo "Чтобы подключиться: tmux attach -t $SESSION_NAME"
echo "Чтобы завершить: tmux kill-session -t $SESSION_NAME"
