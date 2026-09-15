#!/bin/bash
# Run the monitor inside tmux so it can be driven from anywhere -- your terminal,
# another terminal, or a Claude session -- instead of being trapped in whichever
# window happened to launch it.
#
# WHY: every code change needed a manual relaunch in the one terminal that owned
# the process. A tmux session decouples the process from the window: the bot keeps
# running when the window closes, and `restart` works from any shell.
#
# TCC NOTE (the trap this project has hit repeatedly): macOS denies ~/Documents to
# launchd- and cron-spawned children, silently. tmux started from an INTERACTIVE
# shell inherits that grant, which is why this is a tmux wrapper and not a launchd
# agent. Same reasoning as supervise_holder.sh.
set -u
PROJ="/Users/mainfolder/Documents/Hood Sniper"
SESSION="sniper"
ARGF="$PROJ/logs/.sniper_args"
TMUX_BIN="$(command -v tmux)"
cd "$PROJ" || exit 1
mkdir -p logs

# "=name" forces an EXACT session match. Without it tmux matches by PREFIX, so a
# stray session called "snipertest" would answer for "sniper" and commands would
# land on the wrong bot. Note capture-pane/send-keys take a PANE target and reject
# the "=" form, so those use the bare name.
running() { "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null; }

# Auto-attach only makes sense from a real terminal. Run from a script or a Claude
# session there is no tty to attach to, and trying would hang the caller.
interactive() { [ -t 0 ] && [ -t 1 ]; }

cmdline() {
  # Restart must reuse the flags it was STARTED with, or a restart silently drops
  # --watch/--arm and you are running a different bot than you think.
  local saved=""
  [ -f "$ARGF" ] && saved="$(cat "$ARGF")"
  echo "python3 -u scripts/launch_monitor.py $saved"
}

case "${1:-}" in
  start)
    shift
    DETACH=0
    case "${1:-}" in -d|--detached) DETACH=1; shift ;; esac
    if running; then
      # This is not an error, and it should not read like one. From a terminal the
      # useful response to "start it" when it is already up is to SHOW it.
      if [ "$DETACH" = 0 ] && interactive; then
        echo "already running — attaching (ctrl-b then d to leave it running)"
        exec "$TMUX_BIN" attach -t "$SESSION"
      fi
      echo "already running with args: $(cat "$ARGF" 2>/dev/null || echo '<none>')"
      echo "  see it:     scripts/sniper.sh attach"
      echo "  reload it:  scripts/sniper.sh restart"
      exit 0
    fi
    printf '%s' "$*" > "$ARGF"
    "$TMUX_BIN" new-session -d -s "$SESSION" -x 220 -y 55 -c "$PROJ" "$(cmdline)"
    echo "started in tmux session '$SESSION' with args: ${*:-<none>}"
    if [ "$DETACH" = 0 ] && interactive; then
      sleep 1
      exec "$TMUX_BIN" attach -t "$SESSION"     # show it, do not just say it started
    fi
    echo "running in the BACKGROUND (no tty here to attach to)."
    echo "attach with:  tmux attach -t $SESSION      (detach with ctrl-b then d)"
    ;;
  restart)
    if ! running; then echo "not running — starting instead"; exec "$0" start -d $(cat "$ARGF" 2>/dev/null); fi
    # Resolve the exact pane ID first. respawn-pane takes a PANE target and rejects
    # the "=name" exact-session form, so passing it silently failed -- and because
    # the exit code was never checked, this printed "restarted" while the old
    # process kept running. That is the report-success-while-doing-nothing bug this
    # project keeps hitting; see RECURRING-ISSUES.md #2.
    PANE="$("$TMUX_BIN" list-panes -t "=$SESSION" -F '#{pane_id}' | head -1)"
    OLDPID="$("$TMUX_BIN" list-panes -t "=$SESSION" -F '#{pane_pid}' | head -1)"
    if [ -z "$PANE" ]; then echo "restart FAILED: could not resolve a pane"; exit 1; fi
    if ! "$TMUX_BIN" respawn-pane -k -t "$PANE" -c "$PROJ" "$(cmdline)"; then
      echo "restart FAILED: respawn-pane errored (old process still running)"; exit 1
    fi
    sleep 1
    NEWPID="$("$TMUX_BIN" list-panes -t "=$SESSION" -F '#{pane_pid}' | head -1)"
    # assert the OUTCOME, not the exit code: a restart that leaves the same pid did
    # not restart anything
    if [ -n "$NEWPID" ] && [ "$NEWPID" != "$OLDPID" ]; then
      echo "restarted (pid $OLDPID -> $NEWPID) with args: $(cat "$ARGF" 2>/dev/null || echo '<none>')"
    else
      echo "restart FAILED: pid unchanged ($OLDPID) — the old process is still running"; exit 1
    fi
    ;;
  stop)
    running && "$TMUX_BIN" kill-session -t "=$SESSION" && echo "stopped" || echo "was not running"
    ;;
  attach)
    running || { echo "not running — 'sniper.sh start' first"; exit 1; }
    exec "$TMUX_BIN" attach -t "$SESSION"
    ;;
  status)
    if running; then
      echo "RUNNING  args: $(cat "$ARGF" 2>/dev/null || echo '<none>')"
      "$TMUX_BIN" list-panes -t "=$SESSION" -F '  pane pid #{pane_pid}  alive=#{?pane_dead,NO,yes}  #{pane_start_command}'
    else
      echo "STOPPED"
    fi
    ;;
  peek)
    running || { echo "not running"; exit 1; }
    "$TMUX_BIN" capture-pane -p -t "$SESSION"
    ;;
  keys)
    shift
    running || { echo "not running"; exit 1; }
    # -l sends the characters literally, so a key like 'q' is not interpreted as a
    # tmux key name
    "$TMUX_BIN" send-keys -l -t "$SESSION" "$*"
    echo "sent: $*"
    ;;
  *)
    cat <<USAGE
usage: scripts/sniper.sh {start [flags]|restart|stop|attach|status|peek|keys <k>}

  start --watch HOOJA   start it AND open it here (add -d to stay in background)
  restart               relaunch in place, reusing the saved flags
  attach                open it in THIS terminal (ctrl-b then d to leave it running)
  peek                  print the current screen without attaching
  keys s                press a key in it (s sort, v venue, f filter, . hot)
  status / stop
USAGE
    ;;
esac
