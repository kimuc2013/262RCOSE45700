"""CI gating must fail closed; old service stays active when CI is not green."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

SHA = 'a' * 40
NEW = 'b' * 40


@pytest.fixture
def poller(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'deploy' / 'autodeploy.py'
    spec = importlib.util.spec_from_file_location('autodeploy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASE = tmp_path
    monkeypatch.setattr(module, 'latest_sha', lambda: SHA)
    return module


def test_current_release_does_not_call_ci_or_deploy(poller, monkeypatch):
    (poller.BASE / 'current').symlink_to(poller.BASE / SHA)
    monkeypatch.setattr(poller, 'ci_succeeded', lambda sha: pytest.fail('unnecessary API call'))
    poller.tick()


def test_pending_ci_never_downloads_or_deploys(poller, monkeypatch):
    monkeypatch.setattr(poller, 'ci_succeeded', lambda sha: False)
    monkeypatch.setattr(poller, 'get', lambda url: pytest.fail('download before CI passed'))
    poller.tick()
    assert not (poller.BASE / 'autodeploy.failed-sha').exists()


def test_advanced_main_is_deferred(poller, monkeypatch):
    refs = iter([SHA, NEW])
    monkeypatch.setattr(poller, 'latest_sha', lambda: next(refs))
    monkeypatch.setattr(poller, 'ci_succeeded', lambda sha: True)
    monkeypatch.setattr(poller, 'get', lambda url: pytest.fail('outdated commit deployed'))
    poller.tick()


def test_failed_rollout_is_not_retried_until_cleared(poller, monkeypatch):
    monkeypatch.setattr(poller, 'ci_succeeded', lambda sha: True)
    monkeypatch.setattr(poller, 'get', lambda url: '# test deployment')
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(poller.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        poller.tick()
    assert (poller.BASE / 'autodeploy.failed-sha').read_text().strip() == SHA
    monkeypatch.setattr(poller, 'ci_succeeded', lambda sha: pytest.fail('failed SHA retried'))
    poller.tick()


def test_success_requires_expected_release_symlink(poller, monkeypatch):
    monkeypatch.setattr(poller, 'ci_succeeded', lambda sha: True)
    monkeypatch.setattr(poller, 'get', lambda url: '# test deployment')
    monkeypatch.setattr(poller.subprocess, 'run', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match='expected current'):
        poller.tick()
    (poller.BASE / 'autodeploy.failed-sha').unlink()
    def publish(*args, **kwargs):
        (poller.BASE / 'current').symlink_to(poller.BASE / SHA)
    monkeypatch.setattr(poller.subprocess, 'run', publish)
    poller.tick()
    assert not (poller.BASE / 'autodeploy.failed-sha').exists()


@pytest.mark.parametrize('overrides', [
    {}, {'conclusion':'failure'}, {'status':'in_progress'}, {'head_sha':NEW},
    {'head_branch':'feature'}, {'event':'pull_request'}, {'head_repository':{'full_name':'other/repo'}}])
def test_ci_response_must_match_successful_main_push(poller, monkeypatch, overrides):
    import json
    run = {'head_sha':SHA, 'head_branch':'main', 'event':'push', 'status':'completed',
           'conclusion':'success', 'head_repository':{'full_name':poller.REPOSITORY}}
    run.update(overrides)
    monkeypatch.setattr(poller, 'get', lambda url: json.dumps({'workflow_runs':[run]}))
    assert poller.ci_succeeded(SHA) == (overrides == {})
