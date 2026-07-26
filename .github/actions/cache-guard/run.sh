#!/usr/bin/env bash
# The runner behind the root action.yml. Keep inputs in environment variables:
# interpolating workflow input directly into `run:` would turn a path into shell.

set -u

action_path="${GITHUB_ACTION_PATH:?GITHUB_ACTION_PATH is required}"
prompt_file="${INPUT_PROMPT_FILE:-prompts/cache-fixture.jsonl}"
base_ref="${INPUT_BASE_REF:-}"
baseline_file="${INPUT_BASELINE_FILE:-}"
strict="${INPUT_STRICT:-false}"
price_per_mtok="${INPUT_PRICE_PER_MTOK:-3}"
python_bin="${YB5_PYTHON_BIN:-python}"
generated_baseline=""
generated_dir=""

error() {
  printf '::error title=cache_guard input::%s\n' "$1" >&2
}

cleanup() {
  if [[ -n "$generated_baseline" ]]; then
    rm -f -- "$generated_baseline"
  fi
  if [[ -n "$generated_dir" ]]; then
    rmdir -- "$generated_dir"
  fi
}
trap cleanup EXIT

case "$strict" in
  true|false) ;;
  *)
    error "strict must be exactly true or false, got: $strict"
    exit 2
    ;;
esac

if [[ ! -f "$prompt_file" ]]; then
  error "prompt-file does not exist: $prompt_file"
  exit 2
fi
case "$prompt_file" in
  *.json|*.jsonl) ;;
  *)
    error "prompt-file must end in .json or .jsonl: $prompt_file"
    exit 2
    ;;
esac
if [[ -n "$base_ref" && -n "$baseline_file" ]]; then
  error "base-ref and baseline-file are mutually exclusive"
  exit 2
fi
if [[ -n "$baseline_file" && ! -f "$baseline_file" ]]; then
  error "baseline-file does not exist: $baseline_file"
  exit 2
fi

scan_args=(scan)
if [[ "$strict" == "true" ]]; then
  scan_args+=(--strict)
fi

# Do not `set -e`: when scan finds volatility we still run the before/after
# comparison, because its qualified cost estimate is the useful review output.
scan_status=0
"$python_bin" "$action_path/tools/cache_guard.py" \
  "${scan_args[@]}" "$prompt_file" || scan_status=$?

if [[ -n "$base_ref" ]]; then
  base_commit="$(git rev-parse --verify --quiet --end-of-options "${base_ref}^{commit}")"
  if [[ -z "$base_commit" ]]; then
    error "base-ref is not available in this checkout: $base_ref (use checkout fetch-depth: 0)"
    exit 2
  fi
  generated_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/yb5-cache-baseline.XXXXXX")"
  generated_baseline="$generated_dir/baseline.${prompt_file##*.}"
  if ! git show "${base_commit}:${prompt_file}" > "$generated_baseline"; then
    error "prompt-file does not exist at base-ref: ${base_ref}:${prompt_file}"
    exit 2
  fi
  baseline_file="$generated_baseline"
fi

diff_status=0
if [[ -n "$baseline_file" ]]; then
  "$python_bin" "$action_path/tools/cache_guard.py" diff \
    --before "$baseline_file" \
    --after "$prompt_file" \
    --price-per-mtok "$price_per_mtok" || diff_status=$?
else
  printf '%s\n' \
    "::notice title=cache_guard diff skipped::Set base-ref or baseline-file to quantify prefix churn and print a qualified cost estimate."
fi

if (( scan_status > 1 || diff_status > 1 )); then
  error "the guard could not read its inputs (scan=$scan_status, diff=$diff_status)"
  exit 2
fi
if (( scan_status == 1 || diff_status == 1 )); then
  printf '%s\n' \
    "::error title=cacheable prefix regressed::Volatile or changed prefix bytes were found; see the findings and estimate above."
  exit 1
fi

printf '%s\n' "::notice title=cache_guard passed::No cacheable-prefix regression found."
