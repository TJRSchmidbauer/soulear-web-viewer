#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export PYTHONUNBUFFERED=1
export SUEAR_DEBUG="${SUEAR_DEBUG:-1}"
exec python3 suear_mirror.py --no-ssl "$@"
