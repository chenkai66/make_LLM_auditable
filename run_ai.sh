#!/usr/bin/env bash
# Wrapper: export a DashScope key from the shared ecs pool into the env vars the
# seamlens ai layer reads, then run seamlens. The key never lands in any file.
set -euo pipefail
export PYTHONPATH=/root/seamlens_work
eval "$(/usr/bin/python3.8 - <<'PY'
import sys; sys.path.insert(0,'/usr/local/lib/ecs-run')
from ecs_run_keys import KeyPool
p=KeyPool.load()
k,u=p.next(provider='dashscope-intl', model='qwen3.6-flash')
print("export SEAMLENS_AI_BASE_URL=%r" % u)
print("export SEAMLENS_AI_KEY=%r" % k)
PY
)"
cd /root/.minigameforge
exec /usr/bin/python3.8 -m seamlens "$@"
