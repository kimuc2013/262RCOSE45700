"""Send one immutable commit deployment through SSM; no SSH credentials."""
import argparse
import json
import re
import subprocess
import tempfile
import time


def aws(region, *arguments):
    return subprocess.run(['aws', '--region', region, *arguments, '--output', 'json'],
                          text=True, capture_output=True, timeout=60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sha', required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.sha) or not re.fullmatch(r'i-[0-9a-f]{8,17}', args.instance):
        parser.error('Invalid commit SHA or instance ID')
    script = f'''set -eu
script=$(mktemp)
trap 'rm -f "$script"' EXIT
curl --fail --location --retry 2 https://raw.githubusercontent.com/kimuc2013/262RCOSE45700/{args.sha}/serving/deploy/deploy.sh -o "$script"
bash "$script" {args.sha}
'''
    response = aws(args.region, 'ssm', 'send-command', '--instance-ids', args.instance,
        '--document-name', 'AWS-RunShellScript', '--comment', f'KU MCP {args.sha}',
        '--parameters', json.dumps({'commands': [script], 'executionTimeout': ['900']}),
        '--timeout-seconds', '60')
    if response.returncode:
        raise RuntimeError(response.stderr)
    command = json.loads(response.stdout)['Command']['CommandId']
    print(f'SSM command: {command}', flush=True)
    deadline = time.monotonic() + 1000
    while time.monotonic() < deadline:
        result = aws(args.region, 'ssm', 'get-command-invocation', '--command-id', command,
                     '--instance-id', args.instance)
        if result.returncode:
            if 'InvocationDoesNotExist' not in result.stderr:
                raise RuntimeError(result.stderr)
        else:
            data = json.loads(result.stdout)
            status = data['Status']
            if status == 'Success':
                print(data.get('StandardOutputContent', ''))
                return
            if status not in ('Pending', 'InProgress', 'Delayed'):
                raise RuntimeError(f"SSM {status}\n{data.get('StandardOutputContent', '')}\n{data.get('StandardErrorContent', '')}")
        time.sleep(5)
    raise TimeoutError(f'SSM command {command} did not complete; check AWS before retrying')


if __name__ == '__main__':
    main()
