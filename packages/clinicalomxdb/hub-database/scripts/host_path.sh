#!/bin/sh
# Resolve one per-host path from hub-database/.env to the absolute directory compose will bind,
# and print it. One implementation, so the make targets that create these directories and the
# bind that consumes them cannot land on different places.
#
# This exists because the two disagree easily. Compose parses .env as dotenv and then resolves
# the bind source; a shell reading the same line with grep and cut gets the raw text. Each rule
# below is one case where that difference showed, verified against compose v5 by rendering a
# long-syntax bind and reading back the source it resolved to:
#
#   export KEY=./a       the export prefix is accepted
#   KEY=  ./a            surrounding whitespace is dropped
#   KEY=./a #note        in an UNQUOTED value, a SPACE before # starts a comment
#   KEY=./a#b            a # with no space before it is part of the path
#   KEY=./a<TAB>#b       a tab before # does NOT start a comment
#   KEY="./a # kept"     a quoted value keeps its content verbatim, and loses only the quotes
#   KEY="./a" #note      a quoted value ends at its closing quote, and the rest of the line goes
#   KEY=~/a              a leading ~ becomes $HOME
#   KEY=~other/a         compose replaces the ~ whatever follows it, giving $HOME/other/a
#
# A relative value resolves against hub-database/, which is the compose file's own directory and
# not the caller's. An absolute value is used as is.
#
# A value compose itself rejects, such as an unterminated quote, is rejected here too rather than
# guessed at, so the two still refuse the same input.
#
# A VALUE given as the second argument is resolved instead of reading .env, which is how a caller
# holding the environment's own value gets the same tilde and relative rules applied to it. compose
# does not dotenv-parse an environment variable, so the quote and comment rules above are skipped
# for it and only the resolution below runs.
#
# Usage: host_path.sh DB_DATA_DIR [VALUE]
set -eu

key=${1:?usage: host_path.sh KEY [VALUE]}
stack=$(cd "$(dirname "$0")/.." && pwd)

if [ "$#" -ge 2 ]; then
    value=$2
    [ -n "$value" ] || { echo "host_path.sh: $key is empty" >&2; exit 1; }
else
    env_file="$stack/.env"

    [ -f "$env_file" ] || { echo "host_path.sh: $env_file does not exist" >&2; exit 1; }

    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$key=" "$env_file" | tail -1)
    [ -n "$line" ] || { echo "host_path.sh: $key is not set in hub-database/.env" >&2; exit 1; }

    value=${line#*=}
    value=$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

    case $value in
        '"'*)
            rest=${value#\"}
            case $rest in
                *'"'*) value=${rest%%\"*} ;;
                *) echo "host_path.sh: $key has an unterminated quote in hub-database/.env" >&2; exit 1 ;;
            esac
            ;;
        "'"*)
            rest=${value#\'}
            case $rest in
                *"'"*) value=${rest%%\'*} ;;
                *) echo "host_path.sh: $key has an unterminated quote in hub-database/.env" >&2; exit 1 ;;
            esac
            ;;
        *' #'*)
            value=${value%% #*}
            value=$(printf '%s' "$value" | sed -e 's/[[:space:]]*$//')
            ;;
    esac

    [ -n "$value" ] || { echo "host_path.sh: $key is empty in hub-database/.env" >&2; exit 1; }
fi

case $value in
    '~')
        value=$HOME
        ;;
    '~'*)
        rest=${value#\~}
        rest=${rest#/}
        value="$HOME/$rest"
        ;;
esac

case $value in
    /*) ;;
    ./*) value="$stack/${value#./}" ;;
    *)  value="$stack/$value" ;;
esac

printf '%s\n' "$value"
