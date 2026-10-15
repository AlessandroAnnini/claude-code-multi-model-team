#!/usr/bin/env bash
# Multi-model team reminders for Claude Code. No external services, no dependencies.
#
#   team_hook.sh session   -> SessionStart       (plain stdout is added to context)
#   team_hook.sh subagent  -> SubagentStart      (JSON additionalContext)
#   team_hook.sh triage    -> UserPromptSubmit   (keyword hints as JSON additionalContext)
#
# Any failure -> no output -> Claude Code's normal behaviour (fail open).
set -u

case "${1:-}" in
  session)
    cat <<'EOF'
Multi-model team: you are the main session. Delegate repository discovery to the Explore and grep subagents and external documentation to the docs subagent; run independent searches in parallel and state the thoroughness level. Consult the advisor before locking a non-trivial plan, when the same failure occurs twice, and before declaring substantial work done.
EOF
    ;;

  subagent)
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":"You are a read-only research subagent in a multi-model team. Return paths, symbols and concise findings only. Do not modify files, do not make architectural decisions, do not consult the advisor."}}'
    ;;

  triage)
    input=$(cat 2>/dev/null) || exit 0
    # Short prompts ("ok", "go on") get no hints.
    [ "${#input}" -lt 80 ] && exit 0
    lower=$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')
    hints=""
    if printf '%s' "$lower" | grep -qE '\b(where|find|locate|which file|how (does|is|do)|implemented|call(s|ed|er|ers)?|references?|usages?|trace|defined)\b'; then
      hints="Triage: the code involved may not be named in the request; repository discovery via Explore or grep fits here."
    fi
    if printf '%s' "$lower" | grep -qE '\b(library|framework|api|sdk|package|docs|documentation|version|deprecated|migration)\b'; then
      [ -n "$hints" ] && hints="$hints\\n"
      hints="${hints}Triage: the request may depend on external library or API behavior; the docs agent fits here."
    fi
    [ -z "$hints" ] && exit 0
    printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}\n' "$hints"
    ;;
esac
exit 0
