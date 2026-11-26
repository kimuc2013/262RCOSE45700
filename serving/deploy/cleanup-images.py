"""Keep running images and the newest three rebuildable serving images.

Dry-run by default. Never touches volumes, containers, configs, snapshots,
other repositories or images with non-serving tags. Requires Docker access.
"""
import argparse
import json
import re
import subprocess


def command(args):
    return subprocess.run(args,check=True,capture_output=True,text=True).stdout


def select(images, running, keep=3):
    valid=[i for i in images if i.get('RepoTags') and all(
        re.fullmatch(r'ku-academic-mcp:[0-9a-f]{40}',tag) for tag in i['RepoTags'])]
    valid.sort(key=lambda i:i['Created'],reverse=True)
    protected=set(running)|{i['Id'] for i in valid[:keep]}
    return [i for i in valid if i['Id'] not in protected]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--protect-image',action='append',default=[])
    args=parser.parse_args()
    ids=command(['docker','image','ls','--no-trunc','--quiet','--filter','reference=ku-academic-mcp:*']).split()
    images=json.loads(command(['docker','image','inspect',*sorted(set(ids))])) if ids else []
    containers=command(['docker','ps','--all','--quiet']).split()
    running=set(command(['docker','inspect','--format','{{.Image}}',*containers]).split()) if containers else set()
    chosen=select(images,running|set(args.protect_image))
    report={'apply':args.apply,'kept_newest':3,'eligible_images':len(chosen),'eligible_tags':[t for i in chosen for t in i['RepoTags']]}
    print(json.dumps(report),flush=True)
    if args.apply:
        for image in chosen:
            # No force; Docker refuses to remove an image now used by a container.
            command(['docker','image','rm',*image['RepoTags']])
    return report


if __name__=='__main__':main()
