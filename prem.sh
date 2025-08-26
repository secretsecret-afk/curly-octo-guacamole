set -euo pipefail

SESSION="prem"
# рабочая директория — папка скрипта
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

usage() {
  cat <<EOF
Использование: $0 {start|stop|restart|status|attach}
  start   - создать сессию и запустить n.py и sup.py
  stop    - остановить (убить) tmux сессию
  restart - stop затем start
  status  - показать статус (окна) сессии
  attach  - подключиться к сессии (tmux attach)
EOF
}

start() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Сессия '$SESSION' уже запущена."
    return 0
  fi

  mkdir -p "$DIR/logs"

  # создаём сессию и окно для n.py
  tmux new-session -d -s "$SESSION" -n n
  tmux send-keys -t "$SESSION":n "cd '$DIR' && exec $PYTHON n.py >> '$DIR/logs/n.log' 2>&1" C-m

  # создаём окно для sup.py
  tmux new-window -t "$SESSION" -n sup
  tmux send-keys -t "$SESSION":sup "cd '$DIR' && exec $PYTHON sup.py >> '$DIR/logs/sup.log' 2>&1" C-m

  echo "Запущено: сессия '$SESSION' с окнами 'n' и 'sup'. Логи: $DIR/logs/"
}

stop() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    echo "Сессия '$SESSION' остановлена."
  else
    echo "Сессия '$SESSION' не найдена."
  fi
}

status() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Сессия '$SESSION' запущена. Окна:"
    tmux list-windows -t "$SESSION"
  else
    echo "Сессия '$SESSION' не запущена."
  fi
}

attach() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux attach -t "$SESSION"
  else
    echo "Сессия '$SESSION' не найдена. Запустите: $0 start"
    return 1
  fi
}

cmd="${1:-start}"
case "$cmd" in
  start)   start ;;
  stop)    stop ;;
  restart) stop || true; start ;;
  status)  status ;;
  attach)  attach ;;
  *)       usage; exit 2 ;;
esac
