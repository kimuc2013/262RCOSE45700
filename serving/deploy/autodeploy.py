"""Deploy public main only after its GitHub push CI succeeds. Python 3.9+."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlencode

REPOSITORY = 'kimuc2013/262RCOSE45700'
BASE = Path(os.getenv('KU_DEPLOY_ROOT', '/opt/ku-academic-mcp'))


def command(args, timeout=30):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def get(url):
    return command(['curl', '--fail', '--silent', '--show-error', '--location',
                    '--max-time', '25', '--header', 'Accept: application/vnd.github+json',
                    '--header', 'User-Agent: ku-academic-mcp-autodeploy', url])


def latest_sha():
    output = command(['git', 'ls-remote', '--exit-code',
                      f'https://github.com/{REPOSITORY}.git', 'refs/heads/main'])
    match = re.fullmatch(r'([0-9a-f]{40})\s+refs/heads/main\s*', output)
    if not match:
        raise RuntimeError('Invalid main reference')
    return match.group(1)


def ci_succeeded(sha):
    query = urlencode({'branch': 'main', 'event': 'push', 'head_sha': sha, 'per_page': 1})
    data = json.loads(get(f'https://api.github.com/repos/{REPOSITORY}/actions/workflows/serving.yml/runs?{query}'))
    runs = data.get('workflow_runs', [])
    if not runs:
        return False
    run = runs[0]
    return (run.get('head_sha') == sha and run.get('head_branch') == 'main'
            and run.get('event') == 'push' and run.get('status') == 'completed'
            and run.get('conclusion') == 'success'
            and run.get('head_repository', {}).get('full_name') == REPOSITORY)


def tick():
    BASE.mkdir(parents=True, exist_ok=True)
    sha = latest_sha()
    current = BASE / 'current'
    if current.is_symlink() and current.resolve().name == sha:
        print(f'Current: {sha}', flush=True)
        return
    failed = BASE / 'autodeploy.failed-sha'
    if failed.exists() and failed.read_text().strip() == sha:
        print(f'Blocked failed release: {sha}; inspect logs and clear failed-sha to retry', flush=True)
        return
    if not ci_succeeded(sha):
        print(f'Waiting for successful main push CI: {sha}', flush=True)
        return
    # main can change during CI/API lookup. A later tick handles the newer commit.
    if latest_sha() != sha:
        print('Main advanced; defer to next tick', flush=True)
        return
    script = get(f'https://raw.githubusercontent.com/{REPOSITORY}/{sha}/serving/deploy/deploy.sh')
    print(f'Deploying CI-approved commit: {sha}', flush=True)
    # Never retry a broken rollout every two minutes; a new SHA can proceed.
    failed.write_text(sha + '\n')
    with tempfile.TemporaryDirectory(prefix='ku-autodeploy-') as tmp:
        path = Path(tmp) / 'deploy.sh'
        path.write_text(script)
        subprocess.run(['bash', str(path), sha], check=True, timeout=1200)
    if not current.is_symlink() or current.resolve().name != sha:
        raise RuntimeError('Deployment did not publish the expected current release')
    failed.unlink()
    print(f'Auto deployment successful: {sha}', flush=True)


if __name__ == '__main__':
    tick()
