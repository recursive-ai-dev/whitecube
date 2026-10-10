# Sourced once by the private interactive Bash used for a catalog script.
# Keep Bash as the parent so Ctrl+C/Ctrl+Z retain normal terminal job control.
PS1='\[\e[1;37m\]cube\[\e[0m\] \w $ '
if [[ -n ${WHITECUBE_JOB-} ]]; then
    _whitecube_job=$WHITECUBE_JOB
    _whitecube_runner=$WHITECUBE_JOB_RUNNER
    unset WHITECUBE_JOB WHITECUBE_JOB_RUNNER
    python3 "$_whitecube_runner" "$_whitecube_job"
    _whitecube_result=$?
    python3 "$_whitecube_runner" --finish-if-missing "$_whitecube_job" "$_whitecube_result"
    unset _whitecube_job _whitecube_runner
    printf '\n[script finished: %s]\n' "$_whitecube_result"
    unset _whitecube_result
elif [[ -n ${WHITECUBE_START_SCRIPT-} ]]; then
    _whitecube_script=$WHITECUBE_START_SCRIPT
    unset WHITECUBE_START_SCRIPT
    bash -- "$_whitecube_script"
    _whitecube_result=$?
    unset _whitecube_script
    printf '\n[script finished: %s]\n' "$_whitecube_result"
    unset _whitecube_result
fi
