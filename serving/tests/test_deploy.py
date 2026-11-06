"""Run the actual deployment shell with isolated fake Docker, not an EC2 host."""
import json
import os
from pathlib import Path
import subprocess

import pytest

SHA = 'a' * 40
SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'deploy.sh'


@pytest.fixture
def host(tmp_path):
    base = tmp_path / 'deployment'
    release = base / 'releases' / SHA / 'serving'
    release.mkdir(parents=True)
    (release / 'compose.yaml').write_text('services: {}\n')
    old = base / 'releases' / 'previous'
    (old / 'serving').mkdir(parents=True)
    (old / 'serving' / 'compose.yaml').write_text('services: {}\n')
    (base / 'current').symlink_to(old)
    (base / 'config').mkdir()
    (base / 'config' / 'runtime.env').write_text('MODEL_MODE=local\n')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    shim = binaries / 'shim'
    shim.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='id': print('0')
elif name in ('flock','sleep'): pass
elif name=='docker':
    with open(os.environ['FAKE_LOG'],'a') as log:
        log.write(json.dumps({'args':args,'image':os.getenv('ACADEMIC_IMAGE')})+'\\n')
    if args[0]=='inspect': print('sha256:previous')
    elif args[0]=='build' and os.getenv('FAIL_BUILD'): sys.exit(1)
    elif 'run' in args and 'revision())' in args[-1]: print('existing-revision')
    elif 'exec' in args and os.getenv('FAIL_SMOKE') and os.getenv('ACADEMIC_IMAGE','').endswith('a'*40): sys.exit(1)
''')
    shim.chmod(0o755)
    for name in ('id', 'docker', 'flock', 'sleep'):
        (binaries / name).symlink_to(shim)
    # Linux mv supports -T; use a portable stand-in for atomic rename on macOS.
    mv = binaries / 'mv'
    mv.write_text('#!/usr/bin/env python3\nimport os,sys\nos.replace(sys.argv[-2],sys.argv[-1])\n')
    mv.chmod(0o755)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'KU_DEPLOY_ROOT': str(base), 'FAKE_LOG': str(tmp_path / 'docker.jsonl')}
    return base, env


def run(host, **flags):
    base, env = host
    result = subprocess.run(['bash', str(SCRIPT), SHA], env={**env, **flags},
                            capture_output=True, text=True, timeout=10)
    calls = [json.loads(s) for s in Path(env['FAKE_LOG']).read_text().splitlines()]
    return result, calls


def test_deploy_pins_image_checks_mcp_and_preserves_existing_data(host):
    result, calls = run(host)
    assert result.returncode == 0, result.stderr
    assert (host[0] / 'current').resolve().name == SHA
    assert any('academic_mcp.smoke' in c['args'] for c in calls)
    assert not any('academic_mcp.ingest' in c['args'] for c in calls)
    assert [c['image'] for c in calls if 'up' in c['args']] == ['ku-academic-mcp:' + SHA]


def test_failed_health_check_restores_previous_image_and_reports_failure(host):
    result, calls = run(host, FAIL_SMOKE='1')
    assert result.returncode != 0, calls
    assert (host[0] / 'current').resolve().name == 'previous'
    assert [c['image'] for c in calls if 'up' in c['args']] == ['ku-academic-mcp:' + SHA, 'sha256:previous']
    assert 'Previous image health check passed' in result.stdout


def test_build_failure_never_restarts_running_service(host):
    result, calls = run(host, FAIL_BUILD='1')
    assert result.returncode != 0, calls
    assert not any('up' in c['args'] for c in calls)
    assert (host[0] / 'current').resolve().name == 'previous'


def test_ssm_sender_propagates_remote_deployment_failure(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('send_ssm', SCRIPT.with_name('send_ssm.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr('sys.argv', ['send_ssm', '--sha', SHA, '--instance', 'i-1234567890abcdef0', '--region', 'us-east-1'])
    responses = iter([
        subprocess.CompletedProcess([], 0, json.dumps({'Command': {'CommandId': 'test-command'}}), ''),
        subprocess.CompletedProcess([], 0, json.dumps({'Status': 'Failed', 'StandardErrorContent': 'health failed'}), '')])
    monkeypatch.setattr(module, 'aws', lambda *args: next(responses))
    with pytest.raises(RuntimeError, match='health failed'):
        module.main()
